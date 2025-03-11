import os
import sys
import logging
import json
import time
import uuid
import asyncio
from typing import Dict, Any, Optional
from backend.rabbitmq.rabbitmq_service import RabbitMQConsumer, RabbitMQPublisher, CHAT_QUEUE, RESPONSE_QUEUE, BATCH_QUEUE
import aiohttp
from backend.database.db_service import ChatHistoryService, get_db
import redis.asyncio as redis
from backend.core.api.dependencies import get_redis_client
from backend.core.services.ray_service import handle_ray_request
from backend.core.services.context_service import update_context, get_user_context
from backend.core.models.api_models import APIRequest
from backend.core.adapters.ray_adapter import RayServeAdapter

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
RAY_SERVE_URL = os.getenv('RAY_SERVE_URL', 'http://localhost:8000')
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_DB = int(os.getenv('REDIS_DB', 0))

# Redis connection pool
from backend.core.api.dependencies import redis_pool

# Create a global context for managing event loops
class LoopContext:
    def __init__(self):
        self.loop = None
        self.redis_client = None

loop_context = LoopContext()

async def get_redis_connection():
    """Get a Redis client from the connection pool. Always create a new client."""
    return redis.Redis(connection_pool=redis_pool)
async def process_chat_task(task_data):
    """Process chat task from RabbitMQ with streaming support"""
    start_time = time.time()
    try:
        # Extract data from task
        correlation_id = task_data.get('correlation_id')
        payload = task_data.get('kwargs', {}).get('payload', {})
        
        # Convert payload to APIRequest if needed
        if not isinstance(payload, APIRequest) and isinstance(payload, dict):
            payload = APIRequest(**payload)
        
        # Extract fields from payload - use attribute access
        user_id = payload.user_id
        message = payload.message
        conversation_id = payload.conversation_id
        max_tokens = getattr(payload, 'max_tokens', 256)
        temperature = getattr(payload, 'temperature', 0.7)
        stream = getattr(payload, 'stream', False)
        
        # Validate required fields
        if not user_id or not message:
            return {
                "response": "Missing required fields in request.",
                "error": "Invalid payload"
            }
        
        # Get stored context
        redis_client = await get_redis_connection()
        try:
            stored_context = await get_user_context(user_id, redis_client, conversation_id)
        except Exception as context_error:
            logger.warning(f"Error getting context, continuing without it: {context_error}")
            stored_context = []
        
        # Check if stored_context might be too large and truncate if needed
        # Estimate tokens (rough approximation)
        total_token_estimate = sum(len(item.split()) for item in (stored_context or []))
        if total_token_estimate > 2000:  # Keep context within reasonable limits
            logger.warning(f"Context too large ({total_token_estimate} est. tokens), truncating")
            # Keep more recent context by taking the last few items
            stored_context = stored_context[-4:]  # Keep last 4 items (2 exchanges)
        
        if stream:
            # Handle streaming response
            stream_correlation_id = correlation_id
            if not stream_correlation_id:
                stream_correlation_id = str(uuid.uuid4())
                logger.warning(f"Missing correlation_id for streaming task. Generated new one: {stream_correlation_id}")
            
            # For streaming responses
            publisher = RabbitMQPublisher()
            await publisher.initialize()  # Initialize async publisher
            
            try:
                # Prepare Ray payload
                ray_payload = {
                    "messages": [
                        {"role": "system", "content": "You are a helpful assistant."}
                    ],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "stream": True
                }
                
                # Add context as alternating user/assistant messages
                for i, context_item in enumerate(stored_context or []):
                    role = "user" if i % 2 == 0 else "assistant"
                    ray_payload["messages"].append({"role": role, "content": context_item})
                
                # Add current message
                ray_payload["messages"].append({"role": "user", "content": message})
                
                # Get streaming response from Ray Serve
                response_text = ""
                async for chunk in RayServeAdapter.call_ray_serve_stream(ray_payload):
                    if chunk:
                        try:
                            # Parse chunk as JSON if possible
                            chunk_str = chunk.decode('utf-8').strip()
                            if chunk_str:
                                try:
                                    chunk_data = json.loads(chunk_str)
                                    token = chunk_data.get("token", "")
                                    done = chunk_data.get("done", False)
                                    
                                    if token:
                                        response_text += token
                                    
                                    # Publish to RabbitMQ stream
                                    await publisher.publish_message(
                                        queue_name=stream_correlation_id,
                                        message={"token": token, "done": done},
                                        exchange="chat_streams_exchange",
                                        correlation_id=stream_correlation_id
                                    )
                                    
                                    if done:
                                        break
                                except json.JSONDecodeError:
                                    # Not valid JSON, use as raw token
                                    response_text += chunk_str
                                    await publisher.publish_message(
                                        queue_name=stream_correlation_id,
                                        message={"token": chunk_str},
                                        exchange="chat_streams_exchange",
                                        correlation_id=stream_correlation_id
                                    )
                        except Exception as chunk_error:
                            logger.error(f"Error processing chunk: {chunk_error}")
                
                # Update context with full response
                try:
                    await update_context(user_id, message, response_text, conversation_id)
                except Exception as ctx_error:
                    logger.error(f"Context update error (non-critical): {ctx_error}")
                
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
                
                # Send final done signal
                await publisher.publish_message(
                    queue_name=stream_correlation_id,
                    message={"token": "", "done": True},
                    exchange="chat_streams_exchange",
                    correlation_id=stream_correlation_id,
                    priority=5
                )
                
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
            # Handle non-streaming response
            # Prepare the ray_payload using the adapter
            api_request = APIRequest(
                user_id=user_id,
                message=message,
                context=stored_context or [],
                max_tokens=max_tokens,
                temperature=temperature,
                conversation_id=conversation_id,
                stream=False
            )
            
            # Call LLM service through the ray adapter
            result = await handle_ray_request(api_request.dict())
            
            # Extract response
            valid_response = result.get("response", "I apologize, but I couldn't generate a meaningful response.")
            
            # Truncate and clean response
            valid_response = valid_response[:1000].strip()
            
            # Update context
            try:
                await update_context(user_id, message, valid_response, conversation_id)
            except Exception as ctx_error:
                logger.error(f"Context update error (non-critical): {ctx_error}")
            
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

# This function properly handles creating a new event loop for each task
def process_task_in_new_loop(message, correlation_id, task_function):
    """Process a task in a new event loop to avoid loop conflicts"""
    # Create a new event loop for this task
    new_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(new_loop)
    
    # Add correlation_id to message for streaming
    message['correlation_id'] = correlation_id
    
    try:
        # Run the task in the new loop
        result = new_loop.run_until_complete(task_function(message))
        
        # Store the result in Redis
        async def store_result():
            redis_client = await get_redis_connection()
            await redis_client.setex(
                f"response:{correlation_id}",
                300,  # 5 minute TTL
                json.dumps(result)
            )
        
        new_loop.run_until_complete(store_result())
        logger.info(f"Processed task and stored response for correlation_id: {correlation_id}")
        return result
    except Exception as e:
        logger.error(f"Task processing error: {e}", exc_info=True)
        # Store error in Redis
        error_result = {"error": str(e)}
        
        async def store_error():
            redis_client = await get_redis_connection()
            await redis_client.setex(
                f"response:{correlation_id}",
                300,  # 5 minute TTL
                json.dumps(error_result)
            )
        
        new_loop.run_until_complete(store_error())
        return error_result
    finally:
        # Clean up
        new_loop.close()

def callback_wrapper(message, correlation_id):
    """Wrap the callback to handle async functions with proper loop isolation"""
    logger.info(f"Received task with correlation_id: {correlation_id}")
    
    # Get the function name to determine which task processor to use
    function_name = message.get('function')
    
    if function_name == 'process_chat':
        # Process chat task in its own isolated event loop
        return process_task_in_new_loop(message, correlation_id, process_chat_task)
    elif function_name == 'process_batch':
        # Process batch task in its own isolated event loop
        return process_task_in_new_loop(message, correlation_id, process_batch_task)
    else:
        logger.warning(f"Unknown function: {function_name}")
        # Create error result for unknown function
        error_result = {"error": f"Unknown function: {function_name}"}
        
        # Set up a loop for storing the error
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def store_error():
            redis_client = await get_redis_connection()
            await redis_client.setex(
                f"response:{correlation_id}",
                300,  # 5 minute TTL
                json.dumps(error_result)
            )
        
        try:
            loop.run_until_complete(store_error())
        finally:
            loop.close()
        
        return error_result

def start_worker():
    """Start async worker"""
    # Create a main event loop for the consumers
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    async def run_consumers():
        chat_consumer = RabbitMQConsumer(CHAT_QUEUE, callback_wrapper)
        batch_consumer = RabbitMQConsumer(BATCH_QUEUE, callback_wrapper)
        
        # Start both consumers concurrently
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