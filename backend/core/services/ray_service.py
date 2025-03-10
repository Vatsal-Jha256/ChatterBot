# services/ray_service.py
import aiohttp
import logging
from typing import Dict, Any, AsyncGenerator
import requests
from backend.core.config.settings import RAY_SERVE_URL, RABBITMQ_HOST

logger = logging.getLogger(__name__)

async def call_ray_serve_stream(payload: Dict[str, Any]) -> AsyncGenerator[bytes, None]:
    """Stream response from Ray Serve endpoint"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(RAY_SERVE_URL, json=payload) as response:
                async for chunk in response.content.iter_any():
                    yield chunk
    except Exception as e:
        logger.error(f"Ray Serve streaming error: {e}")
        yield b'{"response": "Error communicating with LLM service."}'

async def call_ray_serve(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Make non-streaming request to Ray Serve endpoint"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(RAY_SERVE_URL, json=payload) as response:
                if response.status == 200:
                    result = await response.json()
                    return result
                else:
                    logger.error(f"Ray Serve error: {response.status}")
                    return {"response": "Error communicating with LLM service."}
    except Exception as e:
        logger.error(f"Ray Serve call error: {e}")
        return {"response": "Error communicating with LLM service."}

# Function to route to the appropriate handler based on streaming flag
async def handle_ray_request(payload: Dict[str, Any]):
    """Route request to appropriate handler based on streaming flag"""
    if payload.get("stream", False):
        return call_ray_serve_stream(payload)
    else:
        return await call_ray_serve(payload)