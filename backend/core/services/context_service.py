# services/context_service.py
import redis.asyncio as redis
import logging
from backend.core.config.settings import REDIS_HOST, REDIS_PORT, REDIS_DB
import json
from backend.database.db_service import ChatHistoryService, get_db
from backend.core.utils.compression import compress_text, decompress_text
logger = logging.getLogger(__name__)

# Redis connection pool
redis_pool = None

def init_redis():
    """Initialize Redis connection pool"""
    global redis_pool
    redis_pool = redis.ConnectionPool(
        host=REDIS_HOST, 
        port=REDIS_PORT, 
        db=REDIS_DB
    )
    return redis_pool

async def get_redis_client():
    """Get a Redis client from the pool"""
    if redis_pool is None:
        init_redis()
    async with redis.Redis(connection_pool=redis_pool) as client:
        yield client

async def get_user_context(user_id: str, redis_client: redis.Redis, conversation_id: int = None):
    """Retrieve user context from Redis cache with optional database fallback"""
    try:
        # Try to get from Redis first (for faster access)
        cache_key = f"context_cache:{user_id}"
        if conversation_id:
            cache_key = f"context_cache:{user_id}:{conversation_id}"
            
        cached_context = await redis_client.get(cache_key)
        if cached_context:
            if isinstance(cached_context, bytes):
                cached_context = cached_context.decode('utf-8')

            # Attempt decompression if needed
            if len(cached_context) > 100 or cached_context.startswith('eJ'):
                try:
                    decompressed = decompress_text(cached_context)
                    return json.loads(decompressed)
                except:
                    pass

            # Direct JSON parse fallback
            try:
                return json.loads(cached_context)
            except json.JSONDecodeError:
                logger.error("Invalid JSON context - resetting")
                await redis_client.delete(cache_key)
        
        # If not in Redis, try to get from database
        with get_db() as db:
            context = ChatHistoryService.get_recent_context(db, user_id, max_pairs=3, conversation_id=conversation_id)
            
            # If found in database, update Redis cache
            if context:
                compressed_context = compress_text(json.dumps(context))
                await redis_client.setex(cache_key, 3600, compressed_context)
                
            return context
            
    except Exception as e:
        logger.error(f"Context loading error: {e}")
        return []