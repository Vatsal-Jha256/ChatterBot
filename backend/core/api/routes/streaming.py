from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import StreamingResponse
import aio_pika
import json
import logging
import asyncio
from typing import Optional, AsyncGenerator
from pydantic import BaseModel

from backend.core.models.api_models import APIRequest
from backend.core.api.dependencies import verify_api_key, get_redis_client
from backend.core.api.routes.chat import async_chat
from backend.core.adapters.ray_adapter import RayServeAdapter
from backend.core.config.settings import (
    RABBITMQ_HOST, 
    RABBITMQ_PORT, 
    RABBITMQ_USER, 
    RABBITMQ_PASS, 
    RABBITMQ_VHOST
)

router = APIRouter(tags=["Streaming"])
logger = logging.getLogger(__name__)

@router.post("/stream-chat")
async def stream_chat(
    request: Request,
    payload: APIRequest,
    api_key: str = Depends(verify_api_key)
):
    """Endpoint specifically for streaming chat responses"""
    # Force streaming mode for this endpoint
    payload.stream = True
    # Reuse the async_chat endpoint but ensure streaming
    return await async_chat(request, payload, api_key)

@router.get("/stream/{correlation_id}")
async def stream_response(correlation_id: str, api_key: str = Depends(verify_api_key)):
    """SSE endpoint for receiving streaming tokens from RabbitMQ"""
    
    async def event_generator() -> AsyncGenerator[str, None]:
        connection = None
        try:
            # Connect to RabbitMQ
            connection = await aio_pika.connect_robust(
                host=RABBITMQ_HOST,
                port=RABBITMQ_PORT,
                login=RABBITMQ_USER,
                password=RABBITMQ_PASS,
                virtualhost=RABBITMQ_VHOST
            )
            
            # Create channel and declare exchange
            channel = await connection.channel()
            exchange = await channel.declare_exchange(
                "chat_streams_exchange",
                aio_pika.ExchangeType.TOPIC,
                durable=True
            )
            
            # Create exclusive queue for this streaming session
            queue = await channel.declare_queue(exclusive=True)
            await queue.bind(exchange, routing_key=correlation_id)
            
            # Send initial connection established message
            yield f"data: {json.dumps({'token': '', 'status': 'connected'})}\n\n"
            
            # Set a timeout for the stream (5 minutes)
            timeout = 300  # seconds
            start_time = asyncio.get_event_loop().time()
            
            # Process messages as they arrive
            async with queue.iterator() as queue_iter:
                async for message in queue_iter:
                    async with message.process():
                        # Check timeout
                        current_time = asyncio.get_event_loop().time()
                        if current_time - start_time > timeout:
                            logger.warning(f"Stream timeout for correlation_id: {correlation_id}")
                            yield f"data: {json.dumps({'token': '', 'done': True, 'reason': 'timeout'})}\n\n"
                            yield "data: [DONE]\n\n"
                            return
                            
                        try:
                            # Parse message data
                            data = json.loads(message.body.decode())
                            
                            if "token" in data:
                                # Format as SSE data
                                yield f"data: {json.dumps(data)}\n\n"
                                
                                # If done flag is set, end the stream
                                if data.get("done", False):
                                    yield "data: [DONE]\n\n"
                                    return
                                    
                            elif "error" in data:
                                # Handle error message
                                yield f"data: {json.dumps({'token': '', 'error': data['error'], 'done': True})}\n\n"
                                yield "data: [DONE]\n\n"
                                return
                                
                        except Exception as e:
                            logger.error(f"Message processing error: {e}")
                            yield f"data: {json.dumps({'token': '', 'error': str(e), 'done': True})}\n\n"
                            yield "data: [DONE]\n\n"
                            return
        except Exception as e:
            logger.error(f"Stream connection error: {e}")
            yield f"data: {json.dumps({'token': '', 'error': str(e), 'done': True})}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            # Close connection when done
            if connection:
                await connection.close()
    
    # Return a streaming response with proper headers
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Content-Type": "text/event-stream",
            "X-Accel-Buffering": "no"  # Disable nginx buffering
        }
    )

@router.post("/direct-stream")
async def direct_stream(
    request: Request,
    api_key: str = Depends(verify_api_key)
):
    """Direct streaming from Ray/vLLM without RabbitMQ"""
    
    async def generate_stream():
        try:
            # Parse request body
            body = await request.json()
            
            # Ensure stream flag is set
            body["stream"] = True
            
            # Use the adapter to directly stream from vLLM
            async for chunk in await RayServeAdapter.call_ray_serve_stream(body):
                if chunk:
                    # Format chunk for SSE
                    chunk_str = chunk.decode('utf-8').strip()
                    if chunk_str:
                        try:
                            # Try to parse as JSON
                            chunk_data = json.loads(chunk_str)
                            yield f"data: {json.dumps(chunk_data)}\n\n"
                            
                            # Check for done signal
                            if chunk_data.get("done", False):
                                yield "data: [DONE]\n\n"
                                return
                        except json.JSONDecodeError:
                            # Not valid JSON, send as raw token
                            yield f"data: {json.dumps({'token': chunk_str})}\n\n"
            
            # Final done message
            yield f"data: {json.dumps({'token': '', 'done': True})}\n\n"
            yield "data: [DONE]\n\n"
            
        except Exception as e:
            logger.error(f"Direct stream error: {e}")
            yield f"data: {json.dumps({'token': '', 'error': str(e), 'done': True})}\n\n"
            yield "data: [DONE]\n\n"
    
    return StreamingResponse(
        generate_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Content-Type": "text/event-stream",
            "X-Accel-Buffering": "no"
        }
    )