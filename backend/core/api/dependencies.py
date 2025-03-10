# api/dependencies.py
import redis.asyncio as redis
from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader
import json
import logging
from backend.core.config.settings import API_KEY, REDIS_HOST, REDIS_PORT, REDIS_DB
# from backend.database.db_service import ChatHistoryService, get_db
# from utils.compression import compress_text, decompress_text

# Configure logging
logger = logging.getLogger(__name__)

# Redis connection pool
redis_pool = redis.ConnectionPool(
    host=REDIS_HOST, 
    port=REDIS_PORT, 
    db=REDIS_DB
)

# API Key Authentication
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)

async def get_redis_client():
    """Async Redis client dependency"""
    async with redis.Redis(connection_pool=redis_pool) as client:
        yield client

def verify_api_key(api_key: str = Depends(api_key_header)):
    """API key verification"""
    if api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API Key")
    return api_key

