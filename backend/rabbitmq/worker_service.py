import os
import sys
import logging
import json
import time
import uuid
import asyncio
from typing import Dict, Any

# Import the required modules
from backend.rabbitmq.rabbitmq_service import RabbitMQConsumer, RabbitMQPublisher, CHAT_QUEUE, RESPONSE_QUEUE, BATCH_QUEUE
import requests
import aiohttp

# Import database and redis services
from backend.database.db_service import ChatHistoryService, get_db
import redis.asyncio as redis
from backend.core.api.dependencies import get_redis_client
from backend.core.services.ray_service import handle_ray_request
# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
RAY_SERVE_URL = os.getenv('RAY_SERVE_URL', 'http://localhost:8000/generate')
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_DB = int(os.getenv('REDIS_DB', 0))

# Redis connection pool
redis_pool = redis.ConnectionPool(
    host=REDIS_HOST, 
    port=REDIS_PORT, 
    db=REDIS_DB
)

async def get_user_context(user_id: str, conversation_id=None):
    """Get user context from Redis or database"""
    try:
        redis_client = await get_redis_client()
        
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
                    from backend.core.utils.compression import decompress_text
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
                from backend.core.utils.compression import compress_text
                compressed_context = compress_text(json.dumps(context))
                await redis_client.setex(cache_key, 3600, compressed_context)
                
            return context or []  # Return empty list if context is None
            
    except Exception as e:
        logger.error(f"Context loading error: {e}")
        return []

async def update_context(user_id: str, user_message: str, assistant_response: str, conversation_id=None):
    """Update context in Redis after processing"""
    try:
        redis_client = await get_redis_client()
        
        # Get current context
        context = await get_user_context(user_id, conversation_id)
        if context is None:
            context = []
        
        # Update context
        updated_context = (context + [
            user_message[:500],  # Truncate long user messages
            assistant_response[:500]  # Truncate long responses
        ])[-6:]  # Keep last 3 exchanges
        
        # Store with compression in Redis
        from backend.core.utils.compression import compress_text
        compressed_context = compress_text(json.dumps(updated_context))
        cache_key = f"context_cache:{user_id}"
        if conversation_id:
            cache_key = f"context_cache:{user_id}:{conversation_id}"
            
        await redis_client.setex(
            cache_key,
            3600,
            compressed_context
        )
        
        return True
    except Exception as e:
        logger.error(f"Context update error: {e}")
        return False

async def process_chat_task(task_data):
    """Process chat task from RabbitMQ with streaming support"""
    start_time = time.time()
    
    try:
        # Extract data from task
        payload = task_data.get('kwargs', {}).get('payload', {})
        user_id = payload.get('user_id')
        message = payload.get('message')
        conversation_id = payload.get('conversation_id')
        max_tokens = payload.get('max_tokens', 256)
        temperature = payload.get('temperature', 0.7)
        stream = payload.get('stream', False)
        
        # Validate required fields
        if not user_id or not message:
            return {
                "response": "Missing required fields in request.",
                "error": "Invalid payload"
            }
            
        # Get stored context
        stored_context = await get_user_context(user_id, conversation_id)
        
        if stream:
            # Handle streaming response
            stream_correlation_id = task_data.get('correlation_id')
            if not stream_correlation_id:
                stream_correlation_id = str(uuid.uuid4())
                logger.warning(f"Missing correlation_id for streaming task. Generated new one: {stream_correlation_id}")
            
            # For streaming responses
            publisher = RabbitMQPublisher()
            await publisher.initialize()  # Initialize async publisher
            
            try:
                # Connect to Ray Serve streaming endpoint
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        RAY_SERVE_URL,  # NOT /stream
                        json={
                            "message": message,
                            "context": stored_context or [],  # Ensure not None
                            "max_tokens": max_tokens,
                            "temperature": temperature,
                            "stream": True  # Add stream flag
                        }
                    ) as response:
                        response_text = ""
                        async for chunk in response.content.iter_chunked(64):
                            if chunk:
                                token = chunk.decode()
                                if token.strip():  # Make sure token is not empty
                                    response_text += token
                                    # Use stream_correlation_id for both queue_name and correlation_id
                                    await publisher.publish_message(
                                        queue_name=stream_correlation_id,
                                        message={"token": token},
                                        exchange="chat_streams_exchange",
                                        correlation_id=stream_correlation_id
                                    )
                
                # Send done signal with the stream_correlation_id as queue_name
                await publisher.publish_message(
                    queue_name=stream_correlation_id,
                    message={"token": "", "done": True},
                    exchange="chat_streams_exchange",
                    correlation_id=stream_correlation_id,
                    priority=5
                )
                
                # Update context with full response
                await update_context(user_id, message, response_text, conversation_id)
                
                # Save to database
                processing_time = time.time() - start_time
                user_token_count = len(message.split()) * 1.3
                assistant_token_count = len(response_text.split()) * 1.3
                
                with get_db() as db:
                    db_result = ChatHistoryService.save_message_pair(
                        db,
                        user_id,
                        message,
                        response_text or "No response generated",  # Ensure not empty
                        conversation_id=conversation_id,
                        user_token_count=int(user_token_count),
                        assistant_token_count=int(assistant_token_count),
                        processing_time=processing_time
                    )
                    
                    # Get conversation_id for response
                    if not conversation_id:
                        conversation_id = db_result.get("conversation_id")
                
                return {
                    "response": response_text,
                    "conversation_id": conversation_id,
                    "processing_time": processing_time,
                    "streamed": True
                }
                
            except Exception as e:
                logger.error(f"Stream error: {e}", exc_info=True)
                # Use stream_correlation_id for both queue_name and correlation_id
                try:
                    await publisher.publish_message(
                        queue_name=stream_correlation_id,
                        message={"token": "", "error": str(e), "done": True},  # Include done flag to signal client
                        exchange="chat_streams_exchange",
                        correlation_id=stream_correlation_id
                    )
                except Exception as pub_err:
                    logger.error(f"Failed to publish error message: {pub_err}")
                    
                return {
                    "response": "An error occurred during streaming.",
                    "error": str(e)
                }
            finally:
                await publisher.close()  # Make sure to await the close method
        else:
            # Handle non-streaming response (existing code)
            ray_payload = {
                "context": stored_context or [],  # Ensure not None
                "message": message,
                "max_tokens": max_tokens,
                "temperature": temperature
            }
            
            # Call LLM service
            result = await handle_ray_request(ray_payload)
            
            # Extract response
            valid_response = result.get("response", "I apologize, but I couldn't generate a meaningful response.")
            
            # Truncate and clean response
            valid_response = valid_response[:1000].strip()
            
            # Update context
            await update_context(user_id, message, valid_response, conversation_id)
            
            # Calculate processing time and token counts
            processing_time = time.time() - start_time
            user_token_count = len(message.split()) * 1.3
            assistant_token_count = len(valid_response.split()) * 1.3
            
            # Store in database
            with get_db() as db:
                db_result = ChatHistoryService.save_message_pair(
                    db,
                    user_id,
                    message,
                    valid_response,
                    conversation_id=conversation_id,
                    user_token_count=int(user_token_count),
                    assistant_token_count=int(assistant_token_count),
                    processing_time=processing_time
                )
                
                # Get conversation_id for response
                if not conversation_id:
                    conversation_id = db_result.get("conversation_id")
            
            # Return response
            return {
                "response": valid_response,
                "conversation_id": conversation_id,
                "processing_time": processing_time
            }
    except Exception as e:
        logger.error(f"Chat task processing error: {e}", exc_info=True)
        return {
            "response": "An error occurred while processing your request.",
            "error": str(e)
        }

def callback_wrapper(message, correlation_id):
    """Wrap the callback to handle async functions"""
    logger.info(f"Received task with correlation_id: {correlation_id}")
    # Create a new event loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Process the message
        function_name = message.get('function')
        if function_name == 'process_chat':
            # Run the async function and get the result
            result = loop.run_until_complete(process_chat_task(message))
            # Store result in Redis
            async def store_in_redis():
                redis_client = await get_redis_client()
                await redis_client.setex(
                    f"response:{correlation_id}",
                    300,  # 5 minute TTL
                    json.dumps(result)
                )
            # Run the Redis storage operation
            loop.run_until_complete(store_in_redis())
            logger.info(f"Processed chat task and stored response for correlation_id: {correlation_id}")
        elif function_name == 'process_batch':
            # Run the batch processing function
            result = loop.run_until_complete(process_batch_task(message))
            # Store result in Redis
            async def store_in_redis():
                redis_client = await get_redis_client()
                await redis_client.setex(
                    f"response:{correlation_id}",
                    300,  # 5 minute TTL
                    json.dumps(result)
                )
            # Run the Redis storage operation
            loop.run_until_complete(store_in_redis())
            logger.info(f"Processed batch task and stored response for correlation_id: {correlation_id}")
        else:
            logger.warning(f"Unknown function: {function_name}")
    except Exception as e:
        logger.error(f"Task processing error: {e}", exc_info=True)
        # Store error in Redis
        error_result = {
            "error": str(e)
        }
        async def store_error_in_redis():
            redis_client = await get_redis_client()
            await redis_client.setex(
                f"response:{correlation_id}",
                300,  # 5 minute TTL
                json.dumps(error_result)
            )
        # Run the Redis storage operation
        loop.run_until_complete(store_error_in_redis())
    finally:
        # Don't close the loop here, it might be needed for future tasks
        pass

async def process_batch_task(task_data):
    """Process non-priority batch tasks"""
    try:
        task_type = task_data.get('task_type')
        payload = task_data.get('payload', {})

        if task_type == 'get_history':
            # Process history retrieval
            user_id = payload.get('user_id')
            conversation_id = payload.get('conversation_id')
            limit = payload.get('limit', 50)

            with get_db() as db:
                history = ChatHistoryService.get_conversation_history(
                    db,
                    user_id,
                    conversation_id=conversation_id,
                    limit=limit
                )

            return {"messages": history or []}  # Ensure not None

        if task_type == 'get_conversations':
            # Process conversation list retrieval
            user_id = payload.get('user_id')

            with get_db() as db:
                conversations = ChatHistoryService.get_user_conversations(db, user_id)

            return {"conversations": conversations or []}  # Ensure not None

        if task_type == 'update_title':
            # Process title update
            user_id = payload.get('user_id')
            conversation_id = payload.get('conversation_id')
            title = payload.get('title')

            with get_db() as db:
                result = ChatHistoryService.update_conversation_title(
                    db,
                    user_id,
                    conversation_id,
                    title
                )

            return result or {"success": False}  # Ensure not None

        if task_type == 'delete_conversation':
            # Process conversation deletion
            user_id = payload.get('user_id')
            conversation_id = payload.get('conversation_id')

            with get_db() as db:
                result = ChatHistoryService.delete_conversation(db, user_id, conversation_id)

            return result or {"success": False}  # Ensure not None

        return {"error": f"Unknown task type: {task_type}"}

    except Exception as e:
        logger.error(f"Batch task processing error: {e}", exc_info=True)
        return {"error": str(e)}

def start_worker():
    """Start async worker"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    async def run_consumers():
        chat_consumer = RabbitMQConsumer(CHAT_QUEUE, callback_wrapper)
        batch_consumer = RabbitMQConsumer(BATCH_QUEUE, callback_wrapper)
        
        await asyncio.gather(
            chat_consumer.start_consuming(),
            batch_consumer.start_consuming()
        )
        
    try:
        loop.run_until_complete(run_consumers())
    except KeyboardInterrupt:
        logger.info("Worker stopped")
    finally:
        loop.close()

if __name__ == "__main__":
    try:
        start_worker()
    except KeyboardInterrupt:
        logger.info("Worker service stopped")
    except Exception as e:
        logger.error(f"Worker service error: {e}", exc_info=True)
        sys.exit(1)