"""
Redis cache service for user data.
Provides fast lookups and caching for all user data except usage statistics.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple
from app.redis.client import get_redis
from app.db.models import User
from app.models.user import UserResponse, UserStatus

logger = logging.getLogger(__name__)

# Redis key prefixes
REDIS_KEY_PREFIX_USER = "user:"
REDIS_KEY_PREFIX_USER_BY_ID = "user:id:"
REDIS_KEY_PREFIX_USER_LIST = "user:list:"
REDIS_KEY_PREFIX_USER_COUNT = "user:count:"

# TTL for cache entries (24 hours)
CACHE_TTL = 86400


def _get_user_key(username: str) -> str:
    """Get Redis key for user by username."""
    return f"{REDIS_KEY_PREFIX_USER}{username.lower()}"


def _get_user_id_key(user_id: int) -> str:
    """Get Redis key for user by ID."""
    return f"{REDIS_KEY_PREFIX_USER_BY_ID}{user_id}"


def _serialize_user(user: User) -> Dict[str, Any]:
    """
    Serialize user object to dictionary for Redis storage.
    Excludes usage-related fields that are updated frequently.
    """
    # Convert datetime objects to ISO format strings
    def serialize_value(value):
        if isinstance(value, datetime):
            return value.isoformat() if value else None
        elif hasattr(value, 'value'):  # Enum
            return value.value
        elif hasattr(value, '__dict__'):  # Object with __dict__
            return {k: serialize_value(v) for k, v in value.__dict__.items() if not k.startswith('_')}
        elif isinstance(value, list):
            return [serialize_value(item) for item in value]
        elif isinstance(value, dict):
            return {k: serialize_value(v) for k, v in value.items()}
        return value
    
    user_dict = {
        'id': user.id,
        'username': user.username,
        'status': user.status.value if user.status else None,
        'expire': user.expire,
        'data_limit': user.data_limit,
        'used_traffic': getattr(user, 'used_traffic', 0),  # Include for sorting
        'data_limit_reset_strategy': user.data_limit_reset_strategy.value if user.data_limit_reset_strategy else None,
        'note': user.note,
        'on_hold_timeout': serialize_value(user.on_hold_timeout),
        'on_hold_expire_duration': user.on_hold_expire_duration,
        'auto_delete_in_days': user.auto_delete_in_days,
        'ip_limit': user.ip_limit,
        'flow': user.flow,
        'credential_key': user.credential_key,
        'created_at': serialize_value(user.created_at),
        'edit_at': serialize_value(user.edit_at),
        'last_status_change': serialize_value(user.last_status_change),
        'online_at': serialize_value(user.online_at),
        'sub_updated_at': serialize_value(getattr(user, 'sub_updated_at', None)),
        'admin_id': user.admin_id,
        'service_id': user.service_id,
    }
    
    # Add proxies
    if user.proxies:
        user_dict['proxies'] = [
            {
                'type': proxy.type,
                'settings': proxy.settings if isinstance(proxy.settings, dict) else json.loads(proxy.settings) if isinstance(proxy.settings, str) else {},
            }
            for proxy in user.proxies
        ]
    
    # Add inbounds (excluded_inbounds)
    if user.proxies:
        user_dict['inbounds'] = {}
        for proxy in user.proxies:
            if proxy.excluded_inbounds:
                inbound_tags = [inbound.tag for inbound in proxy.excluded_inbounds]
                user_dict['inbounds'][proxy.type] = inbound_tags
    
    # Add next_plan if exists
    if user.next_plan:
        user_dict['next_plan'] = {
            'data_limit': user.next_plan.data_limit,
            'expire': user.next_plan.expire,
            'add_remaining_traffic': user.next_plan.add_remaining_traffic,
            'fire_on_either': user.next_plan.fire_on_either,
        }
    
    return user_dict


def _deserialize_user(user_dict: Dict[str, Any], db: Optional[Any] = None) -> Optional[User]:
    """
    Deserialize user dictionary from Redis to User object.
    Note: This creates a minimal User object, full deserialization requires DB access.
    """
    if not user_dict:
        return None
    
    try:
        from app.db.models import User as UserModel
        user = UserModel()
        user.id = user_dict.get('id')
        user.username = user_dict.get('username')
        if user_dict.get('status'):
            user.status = UserStatus(user_dict['status'])
        user.expire = user_dict.get('expire')
        user.data_limit = user_dict.get('data_limit')
        if user_dict.get('data_limit_reset_strategy'):
            from app.models.user import UserDataLimitResetStrategy
            user.data_limit_reset_strategy = UserDataLimitResetStrategy(user_dict['data_limit_reset_strategy'])
        user.note = user_dict.get('note')
        user.on_hold_expire_duration = user_dict.get('on_hold_expire_duration')
        user.auto_delete_in_days = user_dict.get('auto_delete_in_days')
        user.ip_limit = user_dict.get('ip_limit', 0)
        user.flow = user_dict.get('flow')
        user.credential_key = user_dict.get('credential_key')
        user.admin_id = user_dict.get('admin_id')
        user.service_id = user_dict.get('service_id')
        user.used_traffic = user_dict.get('used_traffic', 0)
        
        # Parse datetime fields
        if user_dict.get('created_at'):
            user.created_at = datetime.fromisoformat(user_dict['created_at'].replace('Z', '+00:00'))
        if user_dict.get('edit_at'):
            user.edit_at = datetime.fromisoformat(user_dict['edit_at'].replace('Z', '+00:00'))
        if user_dict.get('last_status_change'):
            user.last_status_change = datetime.fromisoformat(user_dict['last_status_change'].replace('Z', '+00:00'))
        if user_dict.get('online_at'):
            user.online_at = datetime.fromisoformat(user_dict['online_at'].replace('Z', '+00:00'))
        if user_dict.get('on_hold_timeout'):
            user.on_hold_timeout = datetime.fromisoformat(user_dict['on_hold_timeout'].replace('Z', '+00:00'))
        if user_dict.get('sub_updated_at'):
            user.sub_updated_at = datetime.fromisoformat(user_dict['sub_updated_at'].replace('Z', '+00:00'))
        
        return user
    except Exception as e:
        logger.error(f"Failed to deserialize user from Redis: {e}")
        return None


def cache_user(user: User) -> bool:
    """
    Cache a user's data in Redis.
    
    Args:
        user: User object to cache
        
    Returns:
        True if cached successfully, False otherwise
    """
    redis_client = get_redis()
    if not redis_client:
        return False
    
    try:
        user_dict = _serialize_user(user)
        username_key = _get_user_key(user.username)
        user_id_key = _get_user_id_key(user.id)
        
        # Cache by username and ID
        user_json = json.dumps(user_dict, default=str)
        redis_client.setex(username_key, CACHE_TTL, user_json)
        redis_client.setex(user_id_key, CACHE_TTL, user_json)
        
        return True
    except Exception as e:
        logger.warning(f"Failed to cache user {user.username}: {e}")
        return False


def get_cached_user(username: Optional[str] = None, user_id: Optional[int] = None, db: Optional[Any] = None) -> Optional[User]:
    """
    Get user from Redis cache.
    
    Args:
        username: Username to lookup
        user_id: User ID to lookup
        db: Database session (for fallback)
        
    Returns:
        User object if found, None otherwise
    """
    redis_client = get_redis()
    if not redis_client:
        return None
    
    try:
        if username:
            key = _get_user_key(username)
        elif user_id:
            key = _get_user_id_key(user_id)
        else:
            return None
        
        user_json = redis_client.get(key)
        if user_json:
            user_dict = json.loads(user_json)
            return _deserialize_user(user_dict, db)
        return None
    except Exception as e:
        logger.debug(f"Failed to get user from cache: {e}")
        return None


def invalidate_user_cache(username: Optional[str] = None, user_id: Optional[int] = None) -> bool:
    """
    Invalidate cached user data.
    
    Args:
        username: Username to invalidate
        user_id: User ID to invalidate
        
    Returns:
        True if invalidated successfully
    """
    redis_client = get_redis()
    if not redis_client:
        return False
    
    try:
        keys_to_delete = []
        if username:
            keys_to_delete.append(_get_user_key(username))
        if user_id:
            keys_to_delete.append(_get_user_id_key(user_id))
        
        if keys_to_delete:
            redis_client.delete(*keys_to_delete)
        
        # Also invalidate list caches
        pattern = f"{REDIS_KEY_PREFIX_USER_LIST}*"
        for key in redis_client.scan_iter(match=pattern):
            redis_client.delete(key)
        
        # Invalidate count caches
        pattern = f"{REDIS_KEY_PREFIX_USER_COUNT}*"
        for key in redis_client.scan_iter(match=pattern):
            redis_client.delete(key)
        
        return True
    except Exception as e:
        logger.warning(f"Failed to invalidate user cache: {e}")
        return False


def cache_user_usage_update(user_id: int, used_traffic_delta: int, online_at: Optional[datetime] = None) -> bool:
    """
    Update user usage in Redis cache (for record_usages).
    This stores usage deltas that will be synced to DB later.
    
    Args:
        user_id: User ID
        used_traffic_delta: Traffic usage delta to add
        online_at: Online timestamp
        
    Returns:
        True if cached successfully
    """
    redis_client = get_redis()
    if not redis_client:
        return False
    
    try:
        usage_key = f"user:usage:{user_id}"
        usage_data = {
            'user_id': user_id,
            'used_traffic_delta': used_traffic_delta,
            'online_at': online_at.isoformat() if online_at else None,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
        redis_client.lpush(usage_key, json.dumps(usage_data))
        redis_client.expire(usage_key, 3600)  # Keep for 1 hour
        return True
    except Exception as e:
        logger.warning(f"Failed to cache user usage update: {e}")
        return False


def get_pending_usage_updates() -> List[Dict[str, Any]]:
    """
    Get all pending usage updates from Redis.
    
    Returns:
        List of usage update dictionaries
    """
    redis_client = get_redis()
    if not redis_client:
        return []
    
    try:
        updates = []
        pattern = "user:usage:*"
        for key in redis_client.scan_iter(match=pattern):
            user_id = int(key.split(':')[-1])
            # Get all pending updates for this user
            while True:
                update_json = redis_client.rpop(key)
                if not update_json:
                    break
                try:
                    update_data = json.loads(update_json)
                    updates.append(update_data)
                except json.JSONDecodeError:
                    continue
        return updates
    except Exception as e:
        logger.error(f"Failed to get pending usage updates: {e}")
        return []


def get_all_users_from_cache(db: Optional[Any] = None) -> List[User]:
    """
    Get all users from Redis cache using optimized batch reading.
    Falls back to DB if Redis is not available or cache is incomplete.
    
    Args:
        db: Database session for fallback
        
    Returns:
        List of all User objects
    """
    redis_client = get_redis()
    if not redis_client:
        return []
    
    try:
        # First, collect all user ID keys using scan_iter (this is necessary)
        user_id_keys = []
        pattern = f"{REDIS_KEY_PREFIX_USER_BY_ID}*"
        for key in redis_client.scan_iter(match=pattern, count=1000):  # Use count for better performance
            user_id_keys.append(key)
        
        if not user_id_keys:
            # If no users found in cache, fallback to DB
            if db:
                logger.info("No users found in Redis cache, loading from database")
                from app.db.models import User as UserModel
                from app.db.crud import get_user_queryset
                db_users = get_user_queryset(db, eager_load=True).all()
                # Cache all users for next time
                for db_user in db_users:
                    cache_user(db_user)
                return db_users
            return []
        
        # Use pipeline to read all users in batch (much faster)
        users = []
        seen_user_ids = set()
        batch_size = 1000  # Process in batches to avoid memory issues
        
        for i in range(0, len(user_id_keys), batch_size):
            batch_keys = user_id_keys[i:i + batch_size]
            pipe = redis_client.pipeline()
            for key in batch_keys:
                pipe.get(key)
            results = pipe.execute()
            
            for user_json in results:
                if not user_json:
                    continue
                try:
                    user_dict = json.loads(user_json)
                    user_id = user_dict.get('id')
                    if user_id and user_id not in seen_user_ids:
                        seen_user_ids.add(user_id)
                        user = _deserialize_user(user_dict, db)
                        if user:
                            users.append(user)
                except Exception as e:
                    logger.debug(f"Failed to deserialize user: {e}")
                    continue
        
        if users:
            logger.debug(f"Loaded {len(users)} users from Redis cache")
            return users
        
        # If no users found in cache, fallback to DB
        if db:
            logger.info("No users found in Redis cache, loading from database")
            from app.db.models import User as UserModel
            from app.db.crud import get_user_queryset
            db_users = get_user_queryset(db, eager_load=True).all()
            # Cache all users for next time
            for db_user in db_users:
                cache_user(db_user)
            return db_users
        
        return []
    
    except Exception as e:
        logger.error(f"Failed to get all users from cache: {e}")
        # Fallback to DB
        if db:
            from app.db.models import User as UserModel
            from app.db.crud import get_user_queryset
            return get_user_queryset(db, eager_load=True).all()
        return []


def get_users_from_cache(
    usernames: Optional[List[str]] = None,
    user_ids: Optional[List[int]] = None,
    db: Optional[Any] = None
) -> List[User]:
    """
    Get multiple users from Redis cache.
    Falls back to DB if not found in cache.
    
    Args:
        usernames: List of usernames to lookup
        user_ids: List of user IDs to lookup
        db: Database session for fallback
        
    Returns:
        List of User objects
    """
    redis_client = get_redis()
    if not redis_client or (not usernames and not user_ids):
        return []
    
    users = []
    missing_usernames = []
    missing_user_ids = []
    
    try:
        # Try to get from cache
        if usernames:
            for username in usernames:
                cached_user = get_cached_user(username=username, db=db)
                if cached_user:
                    users.append(cached_user)
                else:
                    missing_usernames.append(username)
        
        if user_ids:
            for user_id in user_ids:
                cached_user = get_cached_user(user_id=user_id, db=db)
                if cached_user:
                    users.append(cached_user)
                else:
                    missing_user_ids.append(user_id)
        
        # Fallback to DB for missing users
        if db and (missing_usernames or missing_user_ids):
            from app.db.models import User as UserModel
            query = db.query(UserModel).filter(UserModel.status != UserStatus.deleted)
            
            if missing_usernames:
                query = query.filter(UserModel.username.in_(missing_usernames))
            if missing_user_ids:
                query = query.filter(UserModel.id.in_(missing_user_ids))
            
            db_users = query.all()
            for db_user in db_users:
                users.append(db_user)
                # Cache for next time
                cache_user(db_user)
    
    except Exception as e:
        logger.error(f"Failed to get users from cache: {e}")
        # Fallback to DB
        if db:
            from app.db.models import User as UserModel
            query = db.query(UserModel).filter(UserModel.status != UserStatus.deleted)
            if usernames:
                query = query.filter(UserModel.username.in_(usernames))
            if user_ids:
                query = query.filter(UserModel.id.in_(user_ids))
            users = query.all()
    
    return users


def warmup_users_cache() -> Tuple[int, int]:
    """
    Warm up Redis cache with all users' data.
    Loads all users (except deleted) into Redis.
    
    Returns:
        Tuple of (total_users, cached_users)
    """
    redis_client = get_redis()
    if not redis_client:
        logger.info("Redis not available, skipping users cache warmup")
        return (0, 0)
    
    try:
        from app.db import GetDB
        from app.db.crud import get_user_queryset
        
        logger.info("Starting users cache warmup...")
        
        cached_count = 0
        total_count = 0
        
        with GetDB() as db:
            # Load all users (except deleted) with all relationships
            users = get_user_queryset(db, eager_load=True).all()
            total_count = len(users)
            
            # Batch cache operations
            pipe = redis_client.pipeline()
            batch_size = 100
            batch_count = 0
            
            for user in users:
                try:
                    user_dict = _serialize_user(user)
                    username_key = _get_user_key(user.username)
                    user_id_key = _get_user_id_key(user.id)
                    
                    user_json = json.dumps(user_dict, default=str)
                    pipe.setex(username_key, CACHE_TTL, user_json)
                    pipe.setex(user_id_key, CACHE_TTL, user_json)
                    
                    batch_count += 1
                    
                    # Execute batch every batch_size items
                    if batch_count >= batch_size:
                        try:
                            pipe.execute()
                            cached_count += batch_count
                            batch_count = 0
                            pipe = redis_client.pipeline()
                        except Exception as e:
                            logger.warning(f"Error during batch cache warmup: {e}")
                            pipe = redis_client.pipeline()
                            batch_count = 0
                except Exception as e:
                    logger.warning(f"Failed to cache user {user.username}: {e}")
                    continue
            
            # Execute remaining items
            if batch_count > 0:
                try:
                    pipe.execute()
                    cached_count += batch_count
                except Exception as e:
                    logger.warning(f"Error during final batch cache warmup: {e}")
        
        logger.info(f"Users cache warmup completed: {cached_count}/{total_count} users cached")
        return (total_count, cached_count)
        
    except Exception as e:
        logger.error(f"Failed to warmup users cache: {e}", exc_info=True)
        return (0, 0)

