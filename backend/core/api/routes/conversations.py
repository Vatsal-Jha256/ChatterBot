# api/routes/conversations.py
from fastapi import APIRouter, Depends, HTTPException
import redis.asyncio as redis
import uuid
import json
import logging

from backend.core.models.api_models import ConversationRequest, TitleUpdateRequest
from backend.core.api.dependencies import verify_api_key, get_redis_client
from backend.database.db_service import ChatHistoryService, get_db
from backend.rabbitmq.rabbitmq_service import RabbitMQPublisher
from backend.core.config.settings import BATCH_QUEUE

router = APIRouter(tags=["Conversations"])
logger = logging.getLogger(__name__)

@router.get("/conversations/{user_id}")
async def get_user_conversations(
    user_id: str,
    api_key: str = Depends(verify_api_key),
    redis_client: redis.Redis = Depends(get_redis_client)
):
    """Get list of conversations for a user asynchronously"""
    try:
        # Generate correlation ID
        correlation_id = str(uuid.uuid4())
        
        # Create task payload
        task_data = {
            'function': 'process_batch',
            'task_type': 'get_conversations',
            'payload': {
                'user_id': user_id
            }
        }
        
        # Publish to batch queue with low priority using async publisher
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
                conversations = ChatHistoryService.get_user_conversations(db, user_id)
                
            return {"conversations": conversations}
            
        # Check for immediate result (might be cached)
        response_json = await redis_client.get(f"response:{correlation_id}")
        if response_json:
            if isinstance(response_json, bytes):
                response_json = response_json.decode('utf-8')
            return json.loads(response_json)
            
        # No immediate result, return async processing status
        return {
            "status": "processing",
            "message": "Conversation retrieval in progress",
            "correlation_id": correlation_id
        }
        
    except Exception as e:
        logger.error(f"Conversation listing error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/conversations")
async def create_conversation(
    request: ConversationRequest,
    api_key: str = Depends(verify_api_key)
):
    """Create a new conversation"""
    try:
        with get_db() as db:
            result = ChatHistoryService.create_new_conversation(db, request.user_id, request.title)
            
        if result["success"]:
            return result
        else:
            raise HTTPException(status_code=500, detail=result["error"])
            
    except Exception as e:
        logger.error(f"Conversation creation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.put("/conversations/title")
async def update_conversation_title(
    request: TitleUpdateRequest,
    api_key: str = Depends(verify_api_key),
    redis_client: redis.Redis = Depends(get_redis_client)
):
    """Update conversation title asynchronously"""
    try:
        # Generate correlation ID
        correlation_id = str(uuid.uuid4())
        
        # Create task payload
        task_data = {
            'function': 'process_batch',
            'task_type': 'update_title',
            'payload': {
                'user_id': request.user_id,
                'conversation_id': request.conversation_id,
                'title': request.title
            }
        }
        
        # Publish to batch queue with low priority
        publisher = RabbitMQPublisher()
        await publisher.initialize()  # Initialize async publisher
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
                result = ChatHistoryService.update_conversation_title(
                    db, 
                    request.user_id,
                    request.conversation_id,
                    request.title
                )
                
            if result["success"]:
                return result
            else:
                raise HTTPException(status_code=404 if "not found" in result["error"] else 500, detail=result["error"])
                
        # Check for immediate result (might be cached)
        response_json = await redis_client.get(f"response:{correlation_id}")
        if response_json:
            if isinstance(response_json, bytes):
                response_json = response_json.decode('utf-8')
            result = json.loads(response_json)
            if "error" in result:
                raise HTTPException(status_code=404 if "not found" in result["error"] else 500, detail=result["error"])
            return result
            
        # No immediate result, return async processing status
        return {
            "status": "processing",
            "message": "Title update in progress",
            "correlation_id": correlation_id
        }
            
    except Exception as e:
        logger.error(f"Title update error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/conversations/{user_id}/{conversation_id}")
async def delete_conversation(
    user_id: str,
    conversation_id: int,
    api_key: str = Depends(verify_api_key),
    redis_client: redis.Redis = Depends(get_redis_client)
):
    """Delete a conversation asynchronously"""
    try:
        # Generate correlation ID
        correlation_id = str(uuid.uuid4())
        
        # Create task payload
        task_data = {
            'function': 'process_batch',
            'task_type': 'delete_conversation',
            'payload': {
                'user_id': user_id,
                'conversation_id': conversation_id
            }
        }
        
        # Publish to batch queue with low priority
        publisher = RabbitMQPublisher()
        await publisher.initialize()  # Initialize async
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
                result = ChatHistoryService.delete_conversation(db, user_id, conversation_id)
                
            if result["success"]:
                return result
            else:
                raise HTTPException(status_code=404 if "not found" in result["error"] else 500, detail=result["error"])
                
        # Check for immediate result (might be cached)
        response_json = await redis_client.get(f"response:{correlation_id}")
        if response_json:
            if isinstance(response_json, bytes):
                response_json = response_json.decode('utf-8')
            result = json.loads(response_json)
            if "error" in result:
                raise HTTPException(status_code=404 if "not found" in result["error"] else 500, detail=result["error"])
            return result
            
        # No immediate result, return async processing status
        return {
            "status": "processing",
            "message": "Conversation deletion in progress",
            "correlation_id": correlation_id
        }
            
    except Exception as e:
        logger.error(f"Conversation deletion error: {e}")
        raise HTTPException(status_code=500, detail=str(e))