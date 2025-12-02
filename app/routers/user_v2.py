from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.runtime import logger, xray
from app.db import crud, get_db
from app.db.exceptions import UsersLimitReachedError
from app.dependencies import get_validated_user
from app.models.admin import Admin, AdminRole, UserPermission
from app.models.user import (
    UserCreate,
    UserModify,
    UserResponse,
    UserServiceCreate,
    UserStatus,
    UsersResponse,
)
from app.utils import report, responses
from app.utils.credentials import ensure_user_credential_key

router = APIRouter(prefix="/api/v2", tags=["User V2"], responses={401: responses._401})


def _ensure_service_visibility(service, admin: Admin):
    if admin.role in (AdminRole.sudo, AdminRole.full_access):
        return
    if admin.id is None or admin.id not in service.admin_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You're not allowed")


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def add_user_with_service(
    payload: dict,
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: Admin = Depends(Admin.require_active),
):
    """
    Create user. Supports both service-scoped creation (with service_id) and legacy creation without service.
    """
    # Normalize service_id=0 to None to allow "no service" creation
    if payload.get("service_id") == 0:
        payload["service_id"] = None
    admin.ensure_user_permission(UserPermission.create)

    def _ensure_flow_permission(has_flow: bool) -> None:
        if not has_flow:
            return
        if admin.role in (AdminRole.sudo, AdminRole.full_access):
            return
        if admin.permissions.users.set_flow:
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You're not allowed to set user flow.",
        )

    def _ensure_custom_key_permission(has_key: bool) -> None:
        if not has_key:
            return
        if admin.role in (AdminRole.sudo, AdminRole.full_access):
            return
        if admin.permissions.users.allow_custom_key:
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You're not allowed to set a custom credential key.",
        )

    if payload.get("service_id") is not None:
        # Service-scoped creation (existing behavior)
        try:
            service_payload = UserServiceCreate.model_validate(payload)
        except Exception as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

        _ensure_flow_permission(bool(service_payload.flow))
        _ensure_custom_key_permission(bool(service_payload.credential_key))

        service = crud.get_service(db, service_payload.service_id)
        if not service:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")

        _ensure_service_visibility(service, admin)

        db_admin = crud.get_admin(db, admin.username)
        if not db_admin:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin not found")

        allowed_inbounds = crud.get_service_allowed_inbounds(service)
        if not allowed_inbounds:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Service does not have any active hosts")

        proxies_payload = {proxy_type.value: {} for proxy_type in allowed_inbounds.keys()}
        inbounds_payload = {
            proxy_type.value: sorted(list(tags))
            for proxy_type, tags in allowed_inbounds.items()
        }

        user_payload = service_payload.model_dump(exclude={"service_id"}, exclude_none=True)
        user_payload["proxies"] = proxies_payload
        user_payload["inbounds"] = inbounds_payload

        try:
            user_data = UserCreate.model_validate(user_payload)
            admin.ensure_user_constraints(
                status_value=user_data.status.value if user_data.status else None,
                data_limit=user_data.data_limit,
                expire=user_data.expire,
                next_plan=user_data.next_plan.model_dump() if user_data.next_plan else None,
            )
            _ensure_custom_key_permission(bool(user_data.credential_key))
            ensure_user_credential_key(user_data)
            dbuser = crud.create_user(
                db,
                user_data,
                admin=db_admin,
                service=service,
            )
        except UsersLimitReachedError as exc:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists")

        bg.add_task(xray.operations.add_user, dbuser=dbuser)
        user = UserResponse.model_validate(dbuser)
        report.user_created(user=user, user_id=dbuser.id, by=admin, user_admin=dbuser.admin)
        logger.info(f'New user "{dbuser.username}" added via service {service.name}')
        return user

    # Legacy/no-service creation path
    try:
        user_data = UserCreate.model_validate(payload)
        admin.ensure_user_constraints(
            status_value=user_data.status.value if user_data.status else None,
            data_limit=user_data.data_limit,
            expire=user_data.expire,
            next_plan=user_data.next_plan.model_dump() if user_data.next_plan else None,
        )
        _ensure_flow_permission(bool(user_data.flow))
        _ensure_custom_key_permission(bool(user_data.credential_key))
        for proxy_type in user_data.proxies:
            if not xray.config.inbounds_by_protocol.get(proxy_type):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Protocol {proxy_type} is disabled on your server",
                )
        ensure_user_credential_key(user_data)
        dbuser = crud.create_user(
            db,
            user_data,
            admin=crud.get_admin(db, admin.username),
        )
    except UsersLimitReachedError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists")

    bg.add_task(xray.operations.add_user, dbuser=dbuser)
    user = UserResponse.model_validate(dbuser)
    report.user_created(user=user, user_id=dbuser.id, by=admin, user_admin=dbuser.admin)
    logger.info(f'New user "{dbuser.username}" added')
    return user


@router.put("/users/{username}", response_model=UserResponse)
def modify_user_with_service(
    modified_user: UserModify,
    bg: BackgroundTasks,
    db: Session = Depends(get_db),
    dbuser: UsersResponse = Depends(get_validated_user),
    admin: Admin = Depends(Admin.require_active),
):
    # Clean stale inbound references before applying updates
    try:
        crud.prune_user_inbounds(db, dbuser)
    except Exception:
        pass
    for proxy_type in modified_user.proxies:
        if not xray.config.inbounds_by_protocol.get(proxy_type):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Protocol {proxy_type} is disabled on your server",
            )

    old_status = dbuser.status
    service_assignment = None
    service_set = False
    db_admin = None

    if "service_id" in modified_user.model_fields_set:
        service_set = True
        if modified_user.service_id is None and admin.role not in (AdminRole.sudo, AdminRole.full_access):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only sudo admins can remove a user from a service.",
            )
        if modified_user.service_id is not None:
            service = crud.get_service(db, modified_user.service_id)
            if not service:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
            _ensure_service_visibility(service, admin)
            allowed_inbounds = crud.get_service_allowed_inbounds(service)
            if not any(allowed_inbounds.values()):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Selected service has no hosts and cannot be used. Please reconfigure the service.",
                )
            db_admin = crud.get_admin(db, admin.username)
            if not db_admin:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Admin not found")
            service_assignment = service

    admin.ensure_user_constraints(
        status_value=modified_user.status.value if modified_user.status else None,
        data_limit=modified_user.data_limit,
        expire=modified_user.expire,
        next_plan=modified_user.next_plan.model_dump() if modified_user.next_plan else None,
    )
    if "flow" in modified_user.model_fields_set:
        has_flow_value = bool(modified_user.flow)
        if admin.role not in (AdminRole.sudo, AdminRole.full_access):
            if not admin.permissions.users.set_flow:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You're not allowed to set user flow.",
                )
    if "credential_key" in modified_user.model_fields_set and modified_user.credential_key:
        if admin.role not in (AdminRole.sudo, AdminRole.full_access):
            if not admin.permissions.users.allow_custom_key:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You're not allowed to set a custom credential key.",
                )

    try:
        dbuser_obj = crud.update_user(
            db,
            dbuser,
            modified_user,
            service=service_assignment,
            service_set=service_set,
            admin=db_admin,
        )
    except UsersLimitReachedError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    user = UserResponse.model_validate(dbuser_obj)

    if user.status in [UserStatus.active, UserStatus.on_hold]:
        bg.add_task(xray.operations.update_user, dbuser=dbuser_obj)
    else:
        bg.add_task(xray.operations.remove_user, dbuser=dbuser_obj)

    bg.add_task(report.user_updated, user=user, user_admin=dbuser_obj.admin, by=admin)
    logger.info(f'User \"{user.username}\" modified via service-aware endpoint')

    if user.status != old_status:
        bg.add_task(
            report.status_change,
            username=user.username,
            status=user.status,
            user=user,
            user_admin=dbuser_obj.admin,
            by=admin,
        )
        logger.info(
            f'User \"{dbuser_obj.username}\" status changed from {old_status} to {user.status}'
        )

    return user


