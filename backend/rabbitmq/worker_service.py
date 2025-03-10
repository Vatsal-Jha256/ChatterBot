import os
import sys
import logging
import json
import time
import uuid
import asyncio
from typing import Dict, Any, Optional

# Import the required modules
from backend.rabbitmq.rabbitmq_service import RabbitMQConsumer, RabbitMQPublisher, CHAT_QUEUE, RESPONSE_QUEUE, BATCH_QUEUE
import aiohttp

# Import database and redis services
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

# Helper function to get a Redis client
async def get_redis_connection():
    """Get a Redis client from the connection pool"""
    return redis.Redis.from_pool(redis_pool)

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
        
        # Extract fields from payload - use attribute access not dictionary access
        user_id = payload.user_id
        message = payload.message
        conversation_id = payload.conversation_id
        max_tokens = payload.max_tokens if hasattr(payload, 'max_tokens') else 256
        temperature = payload.temperature if hasattr(payload, 'temperature') else 0.7
        stream = payload.stream if hasattr(payload, 'stream') else False
        
        # Validate required fields
        if not user_id or not message:
            return {
                "response": "Missing required fields in request.",
                "error": "Invalid payload"
            }
            
        # Get stored context
        redis_client = await get_redis_connection()
        stored_context = await get_user_context(user_id, redis_client, conversation_id)
        
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
    try:
        # Try to get the current event loop if one is already running
        loop = asyncio.get_event_loop()
    except RuntimeError:
        # If no event loop exists, create one
        loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        # Add correlation_id to message data for streaming
        message['correlation_id'] = correlation_id
        
        # Process the message
        function_name = message.get('function')
        if function_name == 'process_chat':
            # Run the async function and get the result
            result = loop.run_until_complete(process_chat_task(message))
            # Define an async function to store the result in Redis
            async def store_in_redis():
                redis_client = await get_redis_connection()
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
            # Define an async function to store the result in Redis
            async def store_in_redis():
                redis_client = await get_redis_connection()
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
        error_result = {"error": str(e)}
        async def store_error_in_redis():
            redis_client = await get_redis_connection()
            await redis_client.setex(
                f"response:{correlation_id}",
                300,  # 5 minute TTL
                json.dumps(error_result)
            )
        loop.run_until_complete(store_error_in_redis())
    finally:
        # Do not close the loop here, as it might be reused for future tasks
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