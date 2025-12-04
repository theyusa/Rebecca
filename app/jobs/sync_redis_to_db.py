"""
Job to sync Redis usage updates to database.
Reads pending usage updates from Redis and applies them to the database.
"""

import logging
from datetime import datetime, timezone
from collections import defaultdict
from typing import Dict, List, Tuple, Optional

from app.runtime import logger, scheduler
from app.db import GetDB
from app.db.models import User, Admin
from app.redis.user_cache import get_pending_usage_updates
from app.redis.client import get_redis
from config import REDIS_SYNC_INTERVAL, REDIS_ENABLED
from sqlalchemy import update, bindparam

logger = logging.getLogger(__name__)


def sync_usage_updates_to_db():
    """
    Sync pending usage updates from Redis to database.
    This job runs periodically to batch update user usage statistics.
    """
    if not REDIS_ENABLED:
        return
    
    redis_client = get_redis()
    if not redis_client:
        return
    
    try:
        # Get all pending usage updates from Redis
        pending_updates = get_pending_usage_updates()
        
        if not pending_updates:
            return
        
        # Group updates by user_id
        user_updates: Dict[int, Dict[str, any]] = defaultdict(lambda: {
            'used_traffic_delta': 0,
            'online_at': None,
        })
        
        for update_data in pending_updates:
            user_id = update_data.get('user_id')
            if not user_id:
                continue
            
            user_updates[user_id]['used_traffic_delta'] += update_data.get('used_traffic_delta', 0)
            
            # Keep the most recent online_at
            update_online_at = update_data.get('online_at')
            if update_online_at:
                try:
                    update_dt = datetime.fromisoformat(update_online_at.replace('Z', '+00:00'))
                    current_online_at = user_updates[user_id]['online_at']
                    if not current_online_at or update_dt > current_online_at:
                        user_updates[user_id]['online_at'] = update_dt
                except Exception:
                    pass
        
        if not user_updates:
            return
        
        # Apply updates to database
        with GetDB() as db:
            # Prepare batch update data
            users_usage = []
            for user_id, update_info in user_updates.items():
                if update_info['used_traffic_delta'] > 0:
                    users_usage.append({
                        'uid': user_id,
                        'value': update_info['used_traffic_delta'],
                    })
            
            if not users_usage:
                return
            
            # Update users' used_traffic
            stmt = update(User). \
                where(User.id == bindparam('uid')). \
                values(
                    used_traffic=User.used_traffic + bindparam('value'),
                    online_at=bindparam('online_at')
                )
            
            # Add online_at to each update
            for usage in users_usage:
                user_id = usage['uid']
                usage['online_at'] = user_updates[user_id]['online_at'] or datetime.utcnow()
            
            # Execute batch update
            db.execute(stmt, users_usage)
            
            # Update admin usage statistics
            user_ids = [u['uid'] for u in users_usage]
            mapping_rows = (
                db.query(User.id, User.admin_id, User.service_id)
                .filter(User.id.in_(user_ids))
                .all()
            )
            
            user_to_admin_service: Dict[int, Tuple[Optional[int], Optional[int]]] = {
                row[0]: (row[1], row[2]) for row in mapping_rows
            }
            
            admin_usage = defaultdict(int)
            service_usage = defaultdict(int)
            admin_service_usage = defaultdict(int)
            
            for usage in users_usage:
                user_id = usage['uid']
                value = usage['value']
                admin_id, service_id = user_to_admin_service.get(user_id, (None, None))
                
                if admin_id:
                    admin_usage[admin_id] += value
                if service_id:
                    service_usage[service_id] += value
                    if admin_id:
                        admin_service_usage[(admin_id, service_id)] += value
            
            # Update admin usage
            if admin_usage:
                admin_data = [{"b_admin_id": admin_id, "value": value} for admin_id, value in admin_usage.items()]
                admin_update_stmt = (
                    update(Admin)
                    .where(Admin.id == bindparam("b_admin_id"))
                    .values(
                        users_usage=Admin.users_usage + bindparam("value"),
                        lifetime_usage=Admin.lifetime_usage + bindparam("value"),
                    )
                )
                db.execute(admin_update_stmt, admin_data)
            
            # Update service usage (if needed)            
            db.commit()
            
            logger.info(f"Synced {len(users_usage)} user usage updates from Redis to database")
            
    except Exception as e:
        logger.error(f"Failed to sync usage updates from Redis to database: {e}", exc_info=True)


if REDIS_ENABLED:
    scheduler.add_job(
        sync_usage_updates_to_db,
        'interval',
        seconds=REDIS_SYNC_INTERVAL,
        coalesce=True,
        max_instances=1,
        replace_existing=True
    )

