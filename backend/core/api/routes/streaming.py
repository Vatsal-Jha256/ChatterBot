# api/routes/streaming.py
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import StreamingResponse
import aio_pika
import json
import logging
from typing import Optional

from backend.core.models.api_models import APIRequest
from backend.core.api.dependencies import verify_api_key
from backend.core.api.routes.chat import async_chat
from backend.core.config.settings import RABBITMQ_HOST, RABBITMQ_PORT, RABBITMQ_USER, RABBITMQ_PASS, RABBITMQ_VHOST

router = APIRouter(tags=["Streaming"])
logger = logging.getLogger(__name__)

@router.post("/stream-chat")
async def stream_chat(
    request: Request,
    payload: APIRequest,
    api_key: str = Depends(verify_api_key)
):
    """Endpoint specifically for streaming chat"""
    # Similar to async-chat but forces streaming
    payload.stream = True
    return await async_chat(request, payload, api_key)

@router.get("/stream/{correlation_id}")
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