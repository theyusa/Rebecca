"""backfill proxies from credential keys

Revision ID: 1f2e3d4c5b6a
Revises: 0b71839bf061
Create Date: 2025-12-02 03:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.db.models import User, Proxy
from app.models.proxy import ProxyTypes
from app.utils.credentials import runtime_proxy_settings

# revision identifiers, used by Alembic.
revision = "1f2e3d4c5b6a"
down_revision = "0b71839bf061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    session = Session(bind=bind)

    try:
        keyed_users = session.query(User).filter(User.credential_key.isnot(None)).all()
        for user in keyed_users:
            proxies = session.query(Proxy).filter(Proxy.user_id == user.id).all()
            for proxy in proxies:
                proxy_type = proxy.type
                if not proxy_type:
                    continue
                # Work on a copy of settings and strip credential fields so we force regeneration
                settings_data = dict(proxy.settings or {})
                for cred_key in ("id", "uuid", "password"):
                    if cred_key in settings_data:
                        settings_data.pop(cred_key, None)

                try:
                    runtime_settings = runtime_proxy_settings(
                        settings_data, proxy_type, user.credential_key
                    )
                except Exception:
                    continue

                # Persist updated id/password back to JSON settings
                proxy.settings = runtime_settings
        session.commit()
    finally:
        session.close()


def downgrade() -> None:
    # Data backfill; not easily reversible
    pass
