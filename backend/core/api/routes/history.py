# api/routes/history.py
from fastapi import APIRouter, Depends, HTTPException
import redis.asyncio as redis
import uuid
import json
import logging
from typing import Optional

from backend.core.api.dependencies import verify_api_key, get_redis_client
from backend.database.db_service import ChatHistoryService, get_db
from backend.rabbitmq.rabbitmq_service import RabbitMQPublisher
from backend.core.config.settings import BATCH_QUEUE

router = APIRouter(tags=["History"])
logger = logging.getLogger(__name__)

@router.get("/history/{user_id}")
async def get_chat_history(
    user_id: str, 
    conversation_id: Optional[int] = None,
    redis_client: redis.Redis = Depends(get_redis_client),
    api_key: str = Depends(verify_api_key)
):
    """Retrieve user's chat history asynchronously"""
    try:
        # Generate correlation ID
        correlation_id = str(uuid.uuid4())
        
        # Create task payload
        task_data = {
            'function': 'process_batch',
            'task_type': 'get_history',
            'payload': {
                'user_id': user_id,
                'conversation_id': conversation_id,
                'limit': 50
            }
        }

        # Publish to batch queue with low priority using the async publisher
        publisher = RabbitMQPublisher()
        await publisher.initialize()
        publish_success = await publisher.publish_message(
            queue_name=BATCH_QUEUE,
            message=task_data,
            correlation_id=correlation_id,
            priority=1
        )
        await publisher.close()
        
        if not publish_success:
            # Fallback to synchronous processing
            with get_db() as db:
                history = ChatHistoryService.get_conversation_history(
                    db, 
                    user_id, 
                    conversation_id=conversation_id,
                    limit=50
                )
                
            return {"messages": history}
            
        # Check for immediate result (might be cached)
        response_json = await redis_client.get(f"response:{correlation_id}")
        if response_json:
            if isinstance(response_json, bytes):
                response_json = response_json.decode('utf-8')
            return json.loads(response_json)
            
        # No immediate result, return async processing status
        return {
            "status": "processing",
            "message": "History retrieval in progress",
            "correlation_id": correlation_id
        }
    
    except Exception as e:
        logger.error(f"History retrieval error: {e}")
        raise HTTPException(status_code=500, detail=str(e))