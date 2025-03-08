import os
import logging
import time
import zlib
import base64
import json
from typing import List, Optional, Dict, Any
from functools import lru_cache

import redis.asyncio as redis
import uvicorn
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, validator
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
import requests
import asyncio
# Add these imports at the top
import uuid
from rabbitmq_service import RabbitMQPublisher, RabbitMQConsumer, get_rabbitmq_connection, CHAT_QUEUE, RESPONSE_QUEUE, BATCH_QUEUE, ensure_rabbitmq_running
import aiohttp
# Import database models and service
from db_models import get_session_maker, init_db
from db_service import ChatHistoryService, get_db

from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file

# TODO: add dead letter queues for failed messages

RABBITMQ_HOST = os.getenv('RABBITMQ_HOST', 'localhost')
RABBITMQ_PORT = int(os.getenv('RABBITMQ_PORT', 5672))
RABBITMQ_USER = os.getenv('RABBITMQ_USER', 'guest')
RABBITMQ_PASS = os.getenv('RABBITMQ_PASS', 'guest')
RABBITMQ_VHOST = os.getenv('RABBITMQ_VHOST', '/')

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize rate limiter
limiter = Limiter(key_func=get_remote_address)

# Redis and Configuration
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_DB = int(os.getenv('REDIS_DB', 0))

# API Configuration
API_KEY = os.getenv('API_KEY', 'apitest1729')
RAY_SERVE_URL = os.getenv('RAY_SERVE_URL', 'http://localhost:8000/RayLLMInference')

# Redis connection pool
redis_pool = redis.ConnectionPool(
    host=REDIS_HOST, 
    port=REDIS_PORT, 
    db=REDIS_DB
)

# Initialize database
init_db()

# Compression Utility Functions
def compress_text(text: str) -> str:
    """Compress input text using zlib and base64 encoding"""
    try:
        compressed = zlib.compress(text.encode('utf-8'))
        return base64.b64encode(compressed).decode('utf-8')
    except Exception as e:
        logger.error(f"Compression error: {e}")
        return text

def decompress_text(compressed_text: str) -> str:
    """Decompress base64 encoded zlib compressed text"""
    try:
        decoded = base64.b64decode(compressed_text)
        return zlib.decompress(decoded).decode('utf-8')
    except Exception as e:
        logger.error(f"Decompression error: {e}")
        return compressed_text

from pydantic import field_validator
# Update the APIRequest model in api_server.py to include stream flag
class APIRequest(BaseModel):
    user_id: str = Field(..., min_length=3, max_length=50)
    message: str = Field(..., min_length=1, max_length=2000)
    context: Optional[List[str]] = Field(default_factory=list, max_length=10)
    max_tokens: int = Field(default=256, ge=10, le=1024)
    temperature: float = Field(default=0.7, ge=0.1, le=1.0)
    conversation_id: Optional[int] = None
    stream: bool = Field(default=False)  # Flag for streaming response
    
    @field_validator('message')
    def validate_message_length(cls, v):
        """Additional validation for message length and content"""
        if len(v) > 2000:
            raise ValueError("Message exceeds maximum allowed length")
        return v

class ConversationRequest(BaseModel):
    user_id: str = Field(..., min_length=3, max_length=50)
    title: Optional[str] = None

class TitleUpdateRequest(BaseModel):
    user_id: str = Field(..., min_length=3, max_length=50)
    conversation_id: int
    title: str = Field(..., min_length=1, max_length=100)

# FastAPI App Configuration
app = FastAPI(
    title="Advanced LLM Chatbot API",
    description="Enhanced LLM API with Ray Serve, rate limiting, context caching, and database storage",
    version="0.4.0"
)

# Add rate limiting middleware
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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

async def get_user_context(user_id: str, redis_client: redis.Redis, conversation_id: Optional[int] = None):
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

async def call_ray_serve(payload: Dict[str, Any]):
    """Make async request to Ray Serve endpoint"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(RAY_SERVE_URL, json=payload) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.error(f"Ray Serve error: {response.status}")
                    return {"response": "Error communicating with LLM service."}
    except Exception as e:
        logger.error(f"Ray Serve call error: {e}")
        return {"response": "Error communicating with LLM service."}
def format_markdown_response(response_text):
    """Ensure markdown formatting is preserved in the response"""
    # Preserve newlines
    formatted = response_text.replace('\\n', '\n')
    # Ensure proper spacing for markdown lists
    formatted = formatted.replace('\n-', '\n\n-')
    return formatted



from aio_pika import connect_robust

from fastapi.responses import StreamingResponse

@app.post("/stream-chat")
async def stream_chat(
    request: Request,
    payload: APIRequest,
    api_key: str = Depends(verify_api_key)
):
    """Endpoint specifically for streaming chat"""
    # Similar to async-chat but forces streaming
    payload.stream = True
    return await async_chat(request, payload, api_key)


import aio_pika
@app.get("/stream/{correlation_id}")
async def stream_response(correlation_id: str):
    async def event_generator():
        try:
            connection = await aio_pika.connect_robust(
                host=RABBITMQ_HOST,
                port=RABBITMQ_PORT,
                login=RABBITMQ_USER,
                password=RABBITMQ_PASS,
                virtualhost=RABBITMQ_VHOST
            )
            channel = await connection.channel()
            exchange = await channel.declare_exchange(
                "chat_streams_exchange", 
                aio_pika.ExchangeType.TOPIC,
                durable=True
            )
            
            queue = await channel.declare_queue(exclusive=True)
            await queue.bind(exchange, routing_key=correlation_id)

            async with queue.iterator() as queue_iter:
                async for message in queue_iter:
                    async with message.process():
                        try:
                            data = json.loads(message.body.decode())
                            if "token" in data:
                                yield f"data: {json.dumps(data)}\n\n"
                            if data.get("done"):
                                yield "data: [DONE]\n\n"
                                return
                        except Exception as e:
                            logger.error(f"Message error: {e}")
        except Exception as e:
            logger.error(f"Stream error: {e}")
            yield f"error: {str(e)}\n\n"
        finally:
            await connection.close()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )



@app.on_event("startup")
async def startup_event():
    """Initialize RabbitMQ on startup"""
    rabbitmq_running = await ensure_rabbitmq_running()
    if not rabbitmq_running:
        logger.warning("RabbitMQ is not available, fallback mechanisms will be used")

@limiter.limit("10/minute")
@app.post("/chat")
async def advanced_chat(
    request: Request,
    payload: APIRequest,
    redis_client: redis.Redis = Depends(get_redis_client),
    api_key: str = Depends(verify_api_key)
):
    start_time = time.time()
    try:
        # Retrieve stored context
        stored_context = await get_user_context(payload.user_id, redis_client, payload.conversation_id)
        
        # Limit context to prevent over-bloating
        context_list = stored_context[-4:] + payload.context  # Last 4 previous exchanges + current context
        
        # Validate message
        if not payload.message or len(payload.message.strip()) < 1:
            return {"response": "Please provide a valid message."}
        
        # Call Ray Serve for inference
        ray_payload = {
            "context": context_list,
            "message": payload.message,
            "max_tokens": payload.max_tokens,
            "temperature": payload.temperature
        }
        
        result = await call_ray_serve(ray_payload)
        
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
        valid_response = format_markdown_response(valid_response)
        
        # Store in database for long-term persistence
        with get_db() as db:
            db_result = ChatHistoryService.save_message_pair(
                db,
                payload.user_id,
                payload.message,
                valid_response,
                conversation_id=payload.conversation_id,
                user_token_count=int(user_token_count),
                assistant_token_count=int(assistant_token_count),
                processing_time=processing_time
            )
            
            # Include conversation_id in response
            conversation_id = db_result.get("conversation_id", None)
            
        return {
            "response": valid_response,
            "conversation_id": conversation_id
        }
    
    except Exception as e:
        logger.error(f"Chat endpoint error: {e}", exc_info=True)
        return {"response": "An error occurred while processing your request."}

@app.get("/history/{user_id}")
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

@app.get("/conversations/{user_id}")
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
async def process_db_operation(operation, *args, **kwargs):
    """Execute database operations asynchronously using a thread pool"""
    loop = asyncio.get_running_loop()
    
    def run_in_session():
        with get_db() as db:
            return operation(db, *args, **kwargs)
            
    # Run database operation in a thread pool
    return await loop.run_in_executor(None, run_in_session)
@app.post("/conversations")
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

@app.put("/conversations/title")
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

@app.delete("/conversations/{user_id}/{conversation_id}")
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

# Direct access to Ray Serve for advanced use cases
@app.post("/generate")
async def direct_generate(
    request: Request,
    api_key: str = Depends(verify_api_key)
):
    """Direct pass-through to Ray Serve endpoint"""
    try:
        # Get raw request body
        body = await request.json()
        
        # Forward directly to Ray Serve
        result = await call_ray_serve(body)
        
        return result
    except Exception as e:
        logger.error(f"Direct generate error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# New async chat endpoint
@limiter.limit("20/minute")
@app.post("/async-chat")
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
        publish_success = await publisher.publish_message(CHAT_QUEUE, task_data, correlation_id)
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
                "correlation_id": correlation_id
            }
    except Exception as e:
        logger.error(f"Async chat request error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/check-response/{correlation_id}")
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
            return json.loads(response_json)
        else:
            # Response not available yet
            return {"status": "pending", "message": "Response not ready yet"}
    except Exception as e:
        logger.error(f"Check response error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
@app.get("/debug/queue-stats")
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

# Health check endpoint
@app.get("/health")
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

# Global error handler for rate limiting
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)