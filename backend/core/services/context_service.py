# context_service.py
import redis.asyncio as redis
import logging
import json
from typing import List, Optional, Union
import asyncio

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Import from dependencies - assuming these are available
from backend.core.config.settings import REDIS_HOST, REDIS_PORT, REDIS_DB
from backend.core.utils.compression import compress_text, decompress_text
from backend.database.db_service import ChatHistoryService, get_db
from backend.core.api.dependencies import redis_pool

# Constants
CONTEXT_TTL = 3600  # 1 hour cache expiry
MAX_CONTEXT_MESSAGES = 6  # Keep last 3 exchanges (user + assistant)
MAX_MESSAGE_LENGTH = 500  # Truncate messages longer than this


def init_redis():
    """Initialize Redis connection pool if not already initialized"""
    global redis_pool
    if redis_pool is None:
        try:
            redis_pool = redis.ConnectionPool(
                host=REDIS_HOST,
                port=REDIS_PORT,
                db=REDIS_DB,
            )
            logger.info(f"Redis connection pool initialized: {REDIS_HOST}:{REDIS_PORT}")
        except Exception as e:
            logger.error(f"Failed to initialize Redis pool: {e}")
            raise
    return redis_pool


async def get_redis_client() -> redis.Redis:
    """Get a Redis client from the connection pool"""
    if redis_pool is None:
        init_redis()
    return redis.Redis(connection_pool=redis_pool)


def _get_cache_key(user_id: str, conversation_id: Optional[Union[int, str]] = None) -> str:
    """Generate a consistent cache key format"""
    if conversation_id:
        return f"context_cache:{user_id}:{conversation_id}"
    return f"context_cache:{user_id}"


async def get_user_context(
    user_id: str, 
    redis_client: redis.Redis, 
    conversation_id: Optional[Union[int, str]] = None
) -> List[str]:
    """
    Retrieve user conversation context from Redis cache with database fallback
    
    Args:
        user_id: The user's unique identifier
        redis_client: An initialized Redis client
        conversation_id: Optional conversation ID for specific thread context
        
    Returns:
        List of context messages in chronological order (user, assistant alternating)
    """
    try:
        # Generate cache key based on user and optional conversation
        cache_key = _get_cache_key(user_id, conversation_id)
        
        # Try Redis cache first for faster access
        cached_context = await redis_client.get(cache_key)
        if cached_context:
            try:
                # Handle different storage formats (compressed vs uncompressed)
                if isinstance(cached_context, bytes):
                    cached_bytes = cached_context
                else:
                    cached_bytes = cached_context.encode('utf-8') if isinstance(cached_context, str) else cached_context
                
                # Try decompression if it looks compressed
                if len(cached_bytes) > 100 or cached_bytes.startswith(b'eJ'):
                    try:
                        decompressed = decompress_text(cached_bytes.decode('utf-8') 
                                                      if isinstance(cached_bytes, bytes) 
                                                      else cached_bytes)
                        return json.loads(decompressed)
                    except Exception as decomp_error:
                        logger.warning(f"Decompression failed, trying direct parse: {decomp_error}")
                
                # Direct JSON parse if not compressed or decompression failed
                if isinstance(cached_bytes, bytes):
                    return json.loads(cached_bytes.decode('utf-8'))
                return json.loads(cached_bytes)
                
            except json.JSONDecodeError as json_err:
                logger.error(f"Invalid JSON context - resetting: {json_err}")
                await redis_client.delete(cache_key)
            except Exception as parse_err:
                logger.error(f"Error parsing cached context: {parse_err}")
        
        # If cache miss or error, fallback to database
        logger.info(f"Cache miss for {cache_key}, fetching from database")
        with get_db() as db:
            context = ChatHistoryService.get_recent_context(
                db, 
                user_id, 
                max_pairs=MAX_CONTEXT_MESSAGES // 2,  # Pairs = user+assistant messages
                conversation_id=conversation_id
            )
            
            # Update Redis cache if we found context
            if context:
                try:
                    # Compress before storing to save space
                    compressed_context = compress_text(json.dumps(context))
                    await redis_client.setex(
                        cache_key,
                        CONTEXT_TTL,
                        compressed_context
                    )
                    logger.debug(f"Updated cache for {cache_key} with {len(context)} messages")
                except Exception as cache_err:
                    logger.warning(f"Failed to update context cache: {cache_err}")
                    
            return context or []  # Ensure we always return a list
            
    except Exception as e:
        logger.error(f"Context loading error: {e}", exc_info=True)
        return []  # Return empty context on error


async def update_context(
    user_id: str, 
    user_message: str, 
    assistant_response: str, 
    conversation_id: Optional[Union[int, str]] = None
) -> bool:
    """
    Update conversation context in Redis after processing a message exchange
    
    Args:
        user_id: The user's unique identifier
        user_message: The latest user message
        assistant_response: The latest assistant response
        conversation_id: Optional conversation ID for specific thread context
        
    Returns:
        bool: True if update succeeded, False otherwise
    """
    try:
        # Get Redis client from pool
        redis_client = await get_redis_client()
        
        # Get current context
        context = await get_user_context(user_id, redis_client, conversation_id)
        
        # Prepare messages for context (truncate if necessary)
        truncated_user_msg = user_message[:MAX_MESSAGE_LENGTH]
        truncated_assistant_msg = assistant_response[:MAX_MESSAGE_LENGTH]
        
        # Update context with latest exchange while maintaining size limit
        updated_context = (context + [truncated_user_msg, truncated_assistant_msg])[-MAX_CONTEXT_MESSAGES:]
        
        # Compress and store updated context
        cache_key = _get_cache_key(user_id, conversation_id)
        compressed_context = compress_text(json.dumps(updated_context))
        
        await redis_client.setex(
            cache_key,
            CONTEXT_TTL,
            compressed_context
        )
        
        logger.debug(f"Updated context for {cache_key}, now {len(updated_context)} messages")
        return True
        
    except Exception as e:
        logger.error(f"Context update error: {e}", exc_info=True)
        return False


async def clear_user_context(
    user_id: str, 
    conversation_id: Optional[Union[int, str]] = None
) -> bool:
    """
    Clear a user's context from Redis
    
    Args:
        user_id: The user's unique identifier
        conversation_id: Optional conversation ID for specific thread context
        
    Returns:
        bool: True if clearing succeeded, False otherwise
    """
    try:
        redis_client = await get_redis_client()
        cache_key = _get_cache_key(user_id, conversation_id)
        await redis_client.delete(cache_key)
        logger.info(f"Cleared context for {cache_key}")
        return True
    except Exception as e:
        logger.error(f"Error clearing context: {e}")
        return False


async def store_system_prompt(
    user_id: str,
    system_prompt: str,
    conversation_id: Optional[Union[int, str]] = None
) -> bool:
    """
    Store a system prompt for a user/conversation
    
    Args:
        user_id: The user's unique identifier
        system_prompt: The system prompt to store
        conversation_id: Optional conversation ID for specific thread
        
    Returns:
        bool: True if storing succeeded, False otherwise
    """
    try:
        redis_client = await get_redis_client()
        prompt_key = f"system_prompt:{user_id}"
        if conversation_id:
            prompt_key = f"system_prompt:{user_id}:{conversation_id}"
            
        await redis_client.setex(
            prompt_key,
            CONTEXT_TTL * 24,  # Longer TTL for system prompts
            system_prompt
        )
        logger.info(f"Stored system prompt for {prompt_key}")
        return True
    except Exception as e:
        logger.error(f"Error storing system prompt: {e}")
        return False


async def get_system_prompt(
    user_id: str,
    redis_client: redis.Redis,
    conversation_id: Optional[Union[int, str]] = None
) -> Optional[str]:
    """
    Get a system prompt for a user/conversation
    
    Args:
        user_id: The user's unique identifier
        redis_client: An initialized Redis client
        conversation_id: Optional conversation ID for specific thread
        
    Returns:
        str or None: The system prompt if found, None otherwise
    """
    try:
        prompt_key = f"system_prompt:{user_id}"
        if conversation_id:
            prompt_key = f"system_prompt:{user_id}:{conversation_id}"
            
        system_prompt = await redis_client.get(prompt_key)
        if system_prompt:
            if isinstance(system_prompt, bytes):
                return system_prompt.decode('utf-8')
            return system_prompt
        return None
    except Exception as e:
        logger.error(f"Error getting system prompt: {e}")
        return None