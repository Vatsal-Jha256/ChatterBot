# main.py
import os
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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

# Global error handler for rate limiting
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

@app.on_event("startup")
async def startup_event():
    """Initialize services on startup"""
    # Initialize RabbitMQ
    rabbitmq_running = await ensure_rabbitmq_running()
    if not rabbitmq_running:
        logger.warning("RabbitMQ is not available, fallback mechanisms will be used")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)