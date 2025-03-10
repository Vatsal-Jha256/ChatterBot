# api/routes/system.py
from fastapi import APIRouter, Depends, HTTPException
import redis.asyncio as redis
import requests
import logging
import time

from backend.core.api.dependencies import verify_api_key, get_redis_client, redis_pool
from backend.database.db_service import get_db
from backend.rabbitmq.rabbitmq_service import get_rabbitmq_connection
from backend.core.config.settings import RAY_SERVE_URL, CHAT_QUEUE, RESPONSE_QUEUE, BATCH_QUEUE
from backend.core.config.settings import API_KEY, REDIS_HOST, REDIS_PORT, REDIS_DB

router = APIRouter(tags=["System"])
logger = logging.getLogger(__name__)


@router.get("/health")
async def health_check():
    """System health check endpoint"""
    try:
        # Check Ray Serve connectivity
        ray_status = False
        try:
            response = requests.get(RAY_SERVE_URL.replace("RayLLMInference", ""))
            ray_status = response.status_code == 200
        except:
            pass
        
        # Check Redis connectivity
        redis_status = False
        try:
            async with redis.Redis(connection_pool=redis_pool) as client:
                await client.ping()
                redis_status = True
        except:
            pass
        
        # Check database connectivity
        db_status = False
        try:
            with get_db() as db:
                db.execute("SELECT 1")
                db_status = True
        except:
            pass
        
        # Check RabbitMQ connectivity
        rabbitmq_status = False
        try:
            connection = await get_rabbitmq_connection(async_mode=True)
            await connection.close()
            rabbitmq_status = True
        except:
            pass
            
        status = "healthy" if ray_status and redis_status and db_status and rabbitmq_status else "degraded"
        
        return {
            "status": status,
            "components": {
                "ray_serve": "up" if ray_status else "down",
                "redis": "up" if redis_status else "down",
                "database": "up" if db_status else "down",
                "rabbitmq": "up" if rabbitmq_status else "down"
            },
            "timestamp": time.time()
        }
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return {
            "status": "error",
            "error": str(e),
            "timestamp": time.time()
        }

@router.get("/debug/queue-stats")
async def queue_stats(
    api_key: str = Depends(verify_api_key)
):
    """Debug endpoint to get queue statistics"""
    try:
        connection = await get_rabbitmq_connection()
        channel = await connection.channel()
        
        # Get queue stats using aio_pika
        chat_queue = await channel.declare_queue(CHAT_QUEUE, passive=True)
        response_queue = await channel.declare_queue(RESPONSE_QUEUE, passive=True)
        batch_queue = await channel.declare_queue(BATCH_QUEUE, passive=True)
        
        await connection.close()
        
        return {
            "chat_queue": {
                "message_count": chat_queue.declaration_result.message_count,
                "consumer_count": chat_queue.declaration_result.consumer_count
            },
            "response_queue": {
                "message_count": response_queue.declaration_result.message_count,
                "consumer_count": response_queue.declaration_result.consumer_count
            },
            "batch_queue": {
                "message_count": batch_queue.declaration_result.message_count,
                "consumer_count": batch_queue.declaration_result.consumer_count
            }
        }
    except Exception as e:
        logger.error(f"Queue stats error: {e}")
        raise HTTPException(status_code=500, detail=str(e))