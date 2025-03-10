from fastapi import APIRouter, Depends, Request, HTTPException, Response
from slowapi import Limiter
from slowapi.util import get_remote_address
import redis.asyncio as redis
import uuid
import logging
import json
import time
from typing import Optional, Dict, Any

from backend.core.models.api_models import APIRequest
from backend.core.api.dependencies import verify_api_key, get_redis_client
from backend.core.utils.formatting import format_markdown_response
from backend.core.utils.async_utils import process_db_operation
from backend.core.services.ray_service import handle_ray_request
from backend.core.adapters.ray_adapter import RayServeAdapter
from backend.database.db_service import ChatHistoryService, get_db
from backend.core.utils.compression import compress_text
from backend.rabbitmq.rabbitmq_service import RabbitMQPublisher
from backend.core.services.context_service import get_user_context
from backend.core.config.settings import CHAT_QUEUE
from fastapi.responses import StreamingResponse

router = APIRouter(tags=["Chat"])
limiter = Limiter(key_func=get_remote_address)
logger = logging.getLogger(__name__)

@limiter.limit("10/minute")
@router.post("/chat")
async def advanced_chat(
    request: Request,
    payload: APIRequest,
    redis_client: redis.Redis = Depends(get_redis_client),
    api_key: str = Depends(verify_api_key)
):
    start_time = time.time()
    try:
        # Validate request payload
        if not payload.message or len(payload.message.strip()) < 1:
            return {"response": "Please provide a valid message."}
            
        # Retrieve stored context
        stored_context = await get_user_context(payload.user_id, redis_client, payload.conversation_id)
        
        # Limit context to prevent over-bloating
        context_list = stored_context[-4:] + payload.context  # Last 4 previous exchanges + current context
        
        # Update payload with merged context
        payload_copy = payload.copy(update={"context": context_list})
        
        # Call Ray Serve for inference using the adapter service
        result = await handle_ray_request(payload_copy.dict())
        
        # Extract response
        valid_response = result.get("response", "I apologize, but I couldn't generate a meaningful response.")
        
        # Truncate and clean response
        valid_response = valid_response[:1000].strip()
        
        # Update context in Redis
        updated_context = (stored_context + [
            payload.message[:500],  # Truncate long user messages
            valid_response[:500]    # Truncate long responses
        ])[-6:]  # Keep last 3 exchanges
                
        # Store with compression in Redis
        compressed_context = compress_text(json.dumps(updated_context))
        cache_key = f"context_cache:{payload.user_id}"
        if payload.conversation_id:
            cache_key = f"context_cache:{payload.user_id}:{payload.conversation_id}"
            
        await redis_client.setex(
            cache_key,
            3600,
            compressed_context
        )
        
        # Calculate processing time and token counts (approximate)
        processing_time = time.time() - start_time
        user_token_count = len(payload.message.split()) * 1.3  # Rough approximation
        assistant_token_count = len(valid_response.split()) * 1.3  # Rough approximation
        
        # Format response for markdown
        formatted_response = format_markdown_response(valid_response)
        
        # Store in database for long-term persistence
        with get_db() as db:
            db_result = ChatHistoryService.save_message_pair(
                db,
                payload.user_id,
                payload.message,
                valid_response,  # Store unformatted response in DB
                conversation_id=payload.conversation_id,
                user_token_count=int(user_token_count),
                assistant_token_count=int(assistant_token_count),
                processing_time=processing_time
            )
            
            # Include conversation_id in response
            conversation_id = db_result.get("conversation_id", None)
            
        return {
            "response": formatted_response,
            "conversation_id": conversation_id,
            "processing_time": processing_time
        }
    
    except Exception as e:
        logger.error(f"Chat endpoint error: {e}", exc_info=True)
        return {"response": "An error occurred while processing your request."}

@limiter.limit("20/minute")
@router.post("/async-chat")
async def async_chat(
    request: Request,
    payload: APIRequest,
    api_key: str = Depends(verify_api_key)
):
    """Process chat request asynchronously via RabbitMQ with optional streaming"""
    try:
        # Validate message
        if not payload.message or len(payload.message.strip()) < 1:
            return {"response": "Please provide a valid message."}
        
        # Generate correlation ID for tracking this request
        correlation_id = str(uuid.uuid4())
        
        # Create task payload
        task_data = {
            'function': 'process_chat',
            'kwargs': {
                'payload': payload.dict()
            }
        }
        
        # Publish to RabbitMQ
        publisher = RabbitMQPublisher()
        await publisher.initialize()  # Initialize async
        publish_success = await publisher.publish_message(
            queue_name=CHAT_QUEUE,
            message=task_data,
            correlation_id=correlation_id
        )
        await publisher.close()

        if not publish_success:
            # Fallback to async processing with thread pool
            result = await process_db_operation(
                ChatHistoryService.save_message_pair,
                payload.user_id,
                payload.message,
                "I apologize, but the messaging system is currently unavailable. Please try again later.",
                conversation_id=payload.conversation_id
            )
            
            return {
                "response": "I apologize, but the messaging system is currently unavailable. Please try again later.",
                "conversation_id": result.get("conversation_id", None)
            }
        
        if payload.stream:
            # Return streaming setup information
            return {
                "status": "streaming",
                "message": "Connect to the streaming endpoint to receive tokens",
                "correlation_id": correlation_id,
                "stream_endpoint": f"/stream/{correlation_id}"
            }
        else:
            # Return standard async response
            return {
                "status": "processing",
                "message": "Your request is being processed",
                "correlation_id": correlation_id,
                "check_endpoint": f"/check-response/{correlation_id}"
            }
    except Exception as e:
        logger.error(f"Async chat request error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/generate")
async def direct_generate(
    request: Request,
    api_key: str = Depends(verify_api_key)
):
    """Direct access to LLM generation through the Ray adapter"""
    try:
        body = await request.json()
        
        # Check for streaming request
        if body.get("stream", False):
            # For streaming response, use StreamingResponse
            generator = handle_ray_request(body)
            return StreamingResponse(
                generator,
                media_type="application/x-ndjson"
            )
        else:
            # For non-streaming, await the result
            result = await handle_ray_request(body)
            return result
    except Exception as e:
        logger.error(f"Direct generate error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/check-response/{correlation_id}")
async def check_response(
    correlation_id: str,
    redis_client: redis.Redis = Depends(get_redis_client),
    api_key: str = Depends(verify_api_key)
):
    """Check if an async response is available"""
    try:
        # Check Redis for response
        response_json = await redis_client.get(f"response:{correlation_id}")
        
        if response_json:
            # Response found
            if isinstance(response_json, bytes):
                response_json = response_json.decode('utf-8')
                
            # Parse response
            response_data = json.loads(response_json)
            
            # Check if response contains "response" key and format it
            if "response" in response_data:
                response_data["response"] = format_markdown_response(response_data["response"])
                
            # Delete the response from Redis after retrieving it
            await redis_client.delete(f"response:{correlation_id}")
            
            return response_data
        else:
            # Response not available yet
            return {"status": "pending", "message": "Response not ready yet"}
    except Exception as e:
        logger.error(f"Check response error: {e}")
        raise HTTPException(status_code=500, detail=str(e))