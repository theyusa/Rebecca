"""
Functions for managing proxy hosts, users, user templates, nodes, and administrative tasks.
"""

import logging
import secrets
from hashlib import sha256
from copy import deepcopy
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from enum import Enum
import uuid
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple, Union, Literal
from types import SimpleNamespace

import sqlalchemy as sa
from sqlalchemy import and_, case, delete, exists, func, or_, inspect, select
from sqlalchemy.exc import DataError, IntegrityError, OperationalError
from sqlalchemy.orm import Query, Session, joinedload, selectinload
from sqlalchemy.sql.functions import coalesce
from app.db.models import (
    JWT,
    TLS,
    Admin,
    AdminServiceLink,
    AdminUsageLogs,
    AdminApiKey,
    NextPlan,
    MasterNodeState,
    Node,
    NodeUsage,
    NodeUserUsage,
    Proxy,
    ProxyHost,
    ProxyInbound,
    ProxyTypes,
    Service,
    ServiceHostLink,
    System,
    User,
    UserTemplate,
    UserUsageResetLogs,
    XrayConfig,
    excluded_inbounds_association,
    template_inbounds_association,
)
from app.models.admin import AdminRole, AdminStatus
from app.models.admin import AdminCreate, AdminModify, AdminPartialModify, ROLE_DEFAULT_PERMISSIONS
from app.utils.xray_defaults import apply_log_paths, load_legacy_xray_config
from app.utils.credentials import (
    generate_key,
    key_to_uuid,
    normalize_key,
    runtime_proxy_settings,
    serialize_proxy_settings,
    uuid_to_key,
    UUID_PROTOCOLS,
    PASSWORD_PROTOCOLS,
)
from app.models.node import GeoMode, NodeCreate, NodeModify, NodeStatus, NodeUsageResponse
from app.models.proxy import ProxyHost as ProxyHostModify, ProxySettings
from xray_api.types.account import XTLSFlows
from app.models.service import ServiceCreate, ServiceHostAssignment, ServiceModify
from app.models.user import (
    UserCreate,
    UserDataLimitResetStrategy,
    UserModify,
    UserResponse,
    UserStatus,
    UserUsageResponse,
)
from app.models.user_template import UserTemplateCreate, UserTemplateModify
from config import (
    SUB_PROFILE_TITLE,
    SUB_SUPPORT_URL,
    USERS_AUTODELETE_DAYS,
    XRAY_SUBSCRIPTION_PATH,
    XRAY_SUBSCRIPTION_URL_PREFIX,
)
# MasterSettingsService not available in current project structure
from app.db.exceptions import UsersLimitReachedError
MASTER_NODE_NAME = "Master"

_USER_STATUS_ENUM_ENSURED = False

_logger = logging.getLogger(__name__)
_RECORD_CHANGED_ERRNO = 1020
ADMIN_DATA_LIMIT_EXHAUSTED_REASON_KEY = "admin_data_limit_exhausted"

# ============================================================================


def _default_admin_subscription_settings(db: Session) -> dict:
    # MasterSettingsService not available, using config directly
    links = [XRAY_SUBSCRIPTION_URL_PREFIX] if XRAY_SUBSCRIPTION_URL_PREFIX or XRAY_SUBSCRIPTION_URL_PREFIX == "" else [""]
    return {
        "subscription_links": links,
        "subscription_path": XRAY_SUBSCRIPTION_PATH,
        "subscription_template": None,
        "subscription_support_url": SUB_SUPPORT_URL,
        "subscription_title": SUB_PROFILE_TITLE,
    }

def _is_record_changed_error(exc: OperationalError) -> bool:
    orig = getattr(exc, "orig", None)
    if not orig:
        return False
    try:
        err_code = orig.args[0]
    except (AttributeError, IndexError):
        return False
    return err_code == _RECORD_CHANGED_ERRNO

def _get_or_create_xray_config(db: Session) -> XrayConfig:
    if not _xray_config_table_exists(db):
        raise RuntimeError("xray_config table is not available yet")

    config = db.get(XrayConfig, 1)
    if config is None:
        config = XrayConfig(id=1, data=apply_log_paths(load_legacy_xray_config()))
        db.add(config)
        db.commit()
        db.refresh(config)
    return config

def get_xray_config(db: Session) -> Dict[str, Any]:
    if not _xray_config_table_exists(db):
        return apply_log_paths(load_legacy_xray_config())

    config = _get_or_create_xray_config(db)
    return apply_log_paths(config.data or {})

def save_xray_config(db: Session, payload: Dict[str, Any]) -> Dict[str, Any]:
    normalized_payload = apply_log_paths(payload or {})
    config = _get_or_create_xray_config(db)
    config.data = deepcopy(normalized_payload or {})
    db.add(config)
    db.commit()
    db.refresh(config)
    return deepcopy(config.data or {})

def _xray_config_table_exists(db: Session) -> bool:
    bind = db.get_bind()
    if bind is None:
        return False
    try:
        inspector = inspect(bind)
        return inspector.has_table("xray_config")
    except Exception:
        return False

def _ensure_user_deleted_status(db: Session) -> bool:
    """
    Ensure the underlying user status enum (if any) supports the 'deleted' value.

    Returns:
        bool: True if the enum already supported, was updated successfully,
              or does not need updating. False if no automated fix was applied.
    """
    global _USER_STATUS_ENUM_ENSURED

    if _USER_STATUS_ENUM_ENSURED:
        return True

    bind = db.get_bind()
    if bind is None:
        return False

    engine = getattr(bind, "engine", bind)
    dialect = engine.dialect.name

    try:
        inspector = sa.inspect(engine)
        columns = inspector.get_columns("users")
    except Exception:  # pragma: no cover - inspector failure
        return False

    status_column = next((col for col in columns if col.get("name") == "status"), None)
    if not status_column:
        return False

    enum_type = status_column.get("type")
    enum_values = getattr(enum_type, "enums", None)
    if enum_values and "deleted" in enum_values:
        _USER_STATUS_ENUM_ENSURED = True
        return True

    try:
        if dialect == "mysql":
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "ALTER TABLE users MODIFY COLUMN status "
                    "ENUM('active','disabled','limited','expired','on_hold','deleted') NOT NULL"
                )
        elif dialect == "postgresql":
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TYPE userstatus ADD VALUE IF NOT EXISTS 'deleted'")
        else:
            # SQLite and other backends require running the Alembic migration.
            return False
    except Exception:  # pragma: no cover - ALTER failure
        return False

    _USER_STATUS_ENUM_ENSURED = True
    return True

def get_system_usage(db: Session) -> System:
    """
    Retrieves system usage information.

    Args:
        db (Session): Database session.

    Returns:
        System: System usage information.
    """
    return db.query(System).first()

def _get_or_create_jwt_record(db: Session) -> JWT:
    """Helper to get or create JWT record."""
    jwt_record = db.query(JWT).first()
    if jwt_record is None:
        import os
        jwt_record = JWT(
            subscription_secret_key=os.urandom(32).hex(),
            admin_secret_key=os.urandom(32).hex(),
            vmess_mask=os.urandom(16).hex(),
            vless_mask=os.urandom(16).hex(),
        )
        db.add(jwt_record)
        db.commit()
        db.refresh(jwt_record)
    return jwt_record

def get_jwt_secret_key(db: Session) -> str:
    """
    Retrieves the JWT secret key for admin authentication.
    This is a legacy function - use get_admin_secret_key() instead.

    Args:
        db (Session): Database session.

    Returns:
        str: Admin JWT secret key.
    """
    jwt_record = _get_or_create_jwt_record(db)
    if hasattr(jwt_record, 'admin_secret_key') and jwt_record.admin_secret_key:
        return jwt_record.admin_secret_key
    elif hasattr(jwt_record, 'secret_key') and jwt_record.secret_key:
        return jwt_record.secret_key
    else:
        import os
        if not hasattr(jwt_record, 'admin_secret_key'):
            jwt_record.admin_secret_key = os.urandom(32).hex()
            db.commit()
            db.refresh(jwt_record)
        return jwt_record.admin_secret_key

def get_subscription_secret_key(db: Session) -> str:
    """Retrieves the secret key for subscription tokens."""
    return _get_or_create_jwt_record(db).subscription_secret_key

def get_admin_secret_key(db: Session) -> str:
    """Retrieves the secret key for admin authentication tokens."""
    return _get_or_create_jwt_record(db).admin_secret_key

def get_uuid_masks(db: Session) -> dict:
    """
    Retrieves the UUID masks for VMess and VLESS protocols.

    Args:
        db (Session): Database session.

    Returns:
        dict: Dictionary with 'vmess_mask' and 'vless_mask' keys, each containing a 32-character hex string.
    """
    import os
    from sqlalchemy import text

    jwt_record = db.query(JWT).first()
    if jwt_record is None:
        jwt_record = JWT(
            subscription_secret_key=os.urandom(32).hex(),
            admin_secret_key=os.urandom(32).hex(),
        )
        try:
            setattr(jwt_record, "vmess_mask", os.urandom(16).hex())
            setattr(jwt_record, "vless_mask", os.urandom(16).hex())
        except Exception:
            pass
        db.add(jwt_record)
        db.commit()
        try:
            db.refresh(jwt_record)
        except Exception:
            pass

    try:
        vm = getattr(jwt_record, "vmess_mask")
        vl = getattr(jwt_record, "vless_mask")
    except AttributeError:
        try:
            row = db.execute(text("SELECT vmess_mask, vless_mask FROM jwt LIMIT 1")).first()
            if row and row[0] and row[1]:
                return {"vmess_mask": row[0], "vless_mask": row[1]}
        except Exception:
            pass
        return {"vmess_mask": os.urandom(16).hex(), "vless_mask": os.urandom(16).hex()}

    updated = False
    if not vm:
        vm = os.urandom(16).hex()
        try:
            setattr(jwt_record, "vmess_mask", vm)
            updated = True
        except Exception:
            pass
    if not vl:
        vl = os.urandom(16).hex()
        try:
            setattr(jwt_record, "vless_mask", vl)
            updated = True
        except Exception:
            pass
    if updated:
        try:
            db.add(jwt_record)
            db.commit()
        except Exception:
            db.rollback()

    return {"vmess_mask": vm, "vless_mask": vl}

def get_tls_certificate(db: Session) -> TLS:
    """
    Retrieves the TLS certificate.

    Args:
        db (Session): Database session.

    Returns:
        TLS: TLS certificate information.
    """
    return db.query(TLS).first()

