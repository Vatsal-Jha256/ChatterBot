"""
Main entry point for the Advanced LLM Chatbot API.

This module initializes the FastAPI app, sets up middleware, registers routes,
and configures error handling for rate limiting.

"""

import os
import logging
import time
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from backend.core.api.routes.init import register_routes
from backend.core.api.middleware import setup_middleware
from backend.core.config.settings import load_config
# from backend.core.services.ray_service import init_ray
from backend.core.services.context_service import init_redis
from backend.core.utils.logging import setup_logging
import uvicorn
from backend.rabbitmq.rabbitmq_service import ensure_rabbitmq_running
from dotenv import load_dotenv
from datetime import datetime, timedelta

# Load environment variables
load_dotenv()

# Configure logging
setup_logging()
logger = logging.getLogger(__name__)

# Initialize rate limiter
limiter = Limiter(key_func=get_remote_address)

# Create FastAPI app
app = FastAPI(
    title="Advanced LLM Chatbot API",
    description="Enhanced LLM API with Ray Serve, rate limiting, context caching, and database storage",
    version="0.4.0"
)

# Add rate limiting middleware
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

# Setup middleware (CORS, etc.)
setup_middleware(app)

# Register all routes
register_routes(app)

# Custom handler for rate-limited requests with exponential backoff
def exponential_backoff_retry(exceed_time: datetime, retry_attempt: int) -> datetime:
    """
    Calculate the backoff time based on the retry attempt.

    Args:
        exceed_time (datetime): The time when the rate limit was exceeded.
        retry_attempt (int): The number of retry attempts.

    Returns:
        datetime: The retry time with exponential backoff.
    """
    backoff_time = min(2 ** retry_attempt, 60)  # Cap backoff to 60 seconds
    retry_after = exceed_time + timedelta(seconds=backoff_time)
    return retry_after

# Global error handler for rate limiting with exponential backoff
@app.exception_handler(RateLimitExceeded)
async def rate_limit_error_handler(request, exc):
    """
    Custom handler to apply exponential backoff logic.

    Args:
        request (Request): The incoming request.
        exc (RateLimitExceeded): The exception raised when the rate limit is exceeded.

    Returns:
        Response: The response to return to the client.
    """
    limit_exceeded_time = datetime.utcnow()
    
    # Get retry attempt number from request headers (if available, else default to 0)
    retry_attempt = int(request.headers.get('X-Retry-Attempt', 0))
    
    # Calculate retry time with exponential backoff
    retry_after = exponential_backoff_retry(limit_exceeded_time, retry_attempt)
    
    # Send Retry-After header to the client
    headers = {"Retry-After": str(int(retry_after.timestamp()))}
    
    logger.warning(f"Rate limit exceeded. Exponential backoff applied. Retry after: {retry_after}")
    
    # Return response with the appropriate headers
    return JSONResponse(
        status_code=429,
        content={"detail": "Rate limit exceeded. Please try again later."},
        headers=headers,
    )

@app.on_event("startup")
async def startup_event():
    """
    Initialize services on startup.
    """
    # Initialize RabbitMQ
    rabbitmq_running = await ensure_rabbitmq_running()
    if not rabbitmq_running:
        logger.warning("RabbitMQ is not available, fallback mechanisms will be used")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)

