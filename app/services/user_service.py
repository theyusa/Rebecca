"""
User service layer.

Routers call into this module; it decides between Redis and DB,
applies business rules, and keeps caches in sync.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import List, Optional, Union

from app.db import crud, Session
from app.db.models import User
from app.models.user import UserListItem, UserResponse, UserStatus, UsersResponse, UserCreate, UserModify
from app.redis.repositories import user_cache
from app.runtime import logger
from app.utils.subscription_links import build_subscription_links


def _compute_subscription_links(username: str, credential_key: Optional[str]) -> tuple[str, dict]:
    """
    Compute subscription links for list items without constructing heavy models.
    Falls back to empty values on failure.
    """
    try:
        payload = SimpleNamespace(username=username, credential_key=credential_key)
        links = build_subscription_links(payload)
        primary = links.get("primary", "")
        return primary or "", links
    except Exception as exc:
        logger.debug("Failed to build subscription links for %s: %s", username, exc)
        return "", {}


def _map_raw_to_list_item(raw: dict) -> UserListItem:
    subscription_url, subscription_urls = _compute_subscription_links(
        raw.get("username", ""), raw.get("credential_key")
    )
    return UserListItem(
        username=raw.get("username"),
        status=raw.get("status"),
        used_traffic=raw.get("used_traffic") or 0,
        lifetime_used_traffic=raw.get("lifetime_used_traffic") or 0,
        created_at=raw.get("created_at"),
        expire=raw.get("expire"),
        data_limit=raw.get("data_limit"),
        data_limit_reset_strategy=raw.get("data_limit_reset_strategy"),
        online_at=raw.get("online_at"),
        service_id=raw.get("service_id"),
        service_name=raw.get("service_name"),
        admin_id=raw.get("admin_id"),
        admin_username=raw.get("admin_username"),
        subscription_url=subscription_url,
        subscription_urls=subscription_urls,
    )


def _map_user_to_list_item(user: User) -> UserListItem:
    subscription_url, subscription_urls = _compute_subscription_links(
        getattr(user, "username", ""), getattr(user, "credential_key", None)
    )
    return UserListItem(
        username=user.username,
        status=user.status,
        used_traffic=getattr(user, "used_traffic", 0) or 0,
        lifetime_used_traffic=getattr(user, "lifetime_used_traffic", 0) or 0,
        created_at=user.created_at,
        expire=user.expire,
        data_limit=user.data_limit,
        data_limit_reset_strategy=getattr(user, "data_limit_reset_strategy", None),
        online_at=getattr(user, "online_at", None),
        service_id=user.service_id,
        service_name=getattr(user, "service", None).name if getattr(user, "service", None) else None,
        admin_id=user.admin_id,
        admin_username=getattr(user.admin, "username", None) if getattr(user, "admin", None) else None,
        subscription_url=subscription_url,
        subscription_urls=subscription_urls,
    )


def _filter_users_raw(
    users: List[dict],
    *,
    username: Optional[List[str]] = None,
    search: Optional[str] = None,
    status: Optional[Union[UserStatus, str]] = None,
    dbadmin=None,
    owners: Optional[List[str]] = None,
    service_id: Optional[int] = None,
) -> List[dict]:
    def _match(u: dict) -> bool:
        if dbadmin and u.get("admin_id") != dbadmin.id:
            return False
        if owners:
            owner_set = {o.lower() for o in owners}
            admin_username = (u.get("admin_username") or "").lower()
            if admin_username not in owner_set:
                return False
        if username:
            username_set = {n.lower() for n in username}
            if (u.get("username") or "").lower() not in username_set:
                return False
        if status is not None:
            target = status.value if hasattr(status, "value") else status
            if u.get("status") != target:
                return False
        if service_id is not None and u.get("service_id") != service_id:
            return False
        if search:
            s = search.lower()
            hay = " ".join(filter(None, [u.get("username", ""), u.get("note", "")])).lower()
            if s not in hay:
                return False
        return True

    return [u for u in users if _match(u)]


def _sort_users_raw(filtered: List[dict], sort_options) -> None:
    sort_opts = sort_options or []
    if sort_opts:
        for opt in reversed(sort_opts):
            sort_str = str(opt.value).lower()
            reverse = "desc" in sort_str
            if "username" in sort_str:
                filtered.sort(key=lambda u: (u.get("username") or "").lower(), reverse=reverse)
            elif "created_at" in sort_str:
                filtered.sort(key=lambda u: u.get("created_at") or "", reverse=reverse)
            elif "used_traffic" in sort_str:
                filtered.sort(key=lambda u: u.get("used_traffic") or 0, reverse=reverse)
            elif "data_limit" in sort_str:
                filtered.sort(key=lambda u: u.get("data_limit") or 0, reverse=reverse)
            elif "expire" in sort_str:
                filtered.sort(key=lambda u: u.get("expire") or "", reverse=reverse)


def get_users_list(
    db: Session,
    *,
    offset: Optional[int],
    limit: Optional[int],
    username: Optional[List[str]],
    search: Optional[str],
    status: Optional[UserStatus],
    sort,
    advanced_filters,
    service_id: Optional[int],
    dbadmin,
    owners: Optional[List[str]],
    users_limit: Optional[int],
    active_total: Optional[int],
) -> UsersResponse:
    # Try Redis fast path if no advanced filters
    if not advanced_filters:
        try:
            all_users = user_cache.get_users_raw(db=db)
            filtered = _filter_users_raw(
                all_users,
                username=username,
                search=search,
                status=status,
                dbadmin=dbadmin,
                owners=owners,
                service_id=service_id,
            )
            _sort_users_raw(filtered, sort)
            total = len(filtered)
            if offset:
                filtered = filtered[offset:]
            if limit:
                filtered = filtered[:limit]
            if active_total is None and dbadmin:
                active_total = len(
                    [
                        u
                        for u in all_users
                        if u.get("admin_id") == dbadmin.id and u.get("status") == UserStatus.active.value
                    ]
                )
            items = [_map_raw_to_list_item(u) for u in filtered]
            return UsersResponse(
                users=items,
                link_templates={},
                total=total,
                active_total=active_total,
                users_limit=users_limit,
            )
        except Exception as exc:
            logger.debug("Users list fast-path failed, falling back to DB: %s", exc)

    # DB fallback
    users, count = crud.get_users(
        db=db,
        offset=offset,
        limit=limit,
        search=search,
        usernames=username,
        status=status,
        sort=sort,
        advanced_filters=advanced_filters,
        service_id=service_id,
        admin=dbadmin,
        admins=owners,
        return_with_count=True,
    )
    items = [_map_user_to_list_item(u) for u in users]
    if active_total is None and dbadmin:
        active_total = crud.get_users_count(db, status=UserStatus.active, admin=dbadmin)
    return UsersResponse(
        users=items,
        link_templates={},
        total=count,
        active_total=active_total,
        users_limit=users_limit,
    )


def get_user_detail(username: str, db: Session) -> Optional[UserResponse]:
    cached = user_cache.get_user(username=username, db=db)
    if cached:
        try:
            return UserResponse.model_validate(cached)
        except Exception:
            pass
    dbuser = crud.get_user(db, username=username)
    if not dbuser:
        return None
    try:
        user_cache.cache_user(dbuser)
    except Exception:
        pass
    return UserResponse.model_validate(dbuser)


def create_user(db: Session, payload: UserCreate, admin=None, service=None) -> UserResponse:
    dbuser = crud.create_user(db, payload, admin=admin, service=service)
    try:
        user_cache.cache_user(dbuser)
    except Exception:
        pass
    return UserResponse.model_validate(dbuser)


def update_user(db: Session, dbuser: User, payload: UserModify) -> UserResponse:
    updated = crud.update_user(db, dbuser, payload)
    try:
        user_cache.cache_user(updated)
    except Exception:
        pass
    return UserResponse.model_validate(updated)


def delete_user(db: Session, dbuser: User):
    crud.remove_user(db, dbuser)
    try:
        user_cache.invalidate_user(username=dbuser.username, user_id=dbuser.id)
    except Exception:
        pass
    return dbuser
