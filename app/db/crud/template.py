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


