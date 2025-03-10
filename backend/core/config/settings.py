# config/settings.py
import os
from dotenv import load_dotenv

# Configuration constants
# Redis Configuration
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_DB = int(os.getenv('REDIS_DB', 0))

# API Configuration
API_KEY = os.getenv('API_KEY', 'apitest1729')
RAY_SERVE_URL = os.getenv('RAY_SERVE_URL', 'http://localhost:8000/generate')

# RabbitMQ Configuration
RABBITMQ_HOST = os.getenv('RABBITMQ_HOST', 'localhost')
RABBITMQ_PORT = int(os.getenv('RABBITMQ_PORT', 5672))
RABBITMQ_USER = os.getenv('RABBITMQ_USER', 'guest')
RABBITMQ_PASS = os.getenv('RABBITMQ_PASS', 'guest')
RABBITMQ_VHOST = os.getenv('RABBITMQ_VHOST', '/')

# Queue Names
CHAT_QUEUE = "chat_requests"
RESPONSE_QUEUE = "chat_responses"
BATCH_QUEUE = "batch_operations"

def load_config():
    """Load configuration from environment variables"""
    load_dotenv()
    # Return a dict of configurations
    return {
        "redis": {
            "host": REDIS_HOST,
            "port": REDIS_PORT,
            "db": REDIS_DB
        },
        "api": {
            "key": API_KEY,
            "ray_serve_url": RAY_SERVE_URL
        },
        "rabbitmq": {
            "host": RABBITMQ_HOST,
            "port": RABBITMQ_PORT,
            "user": RABBITMQ_USER,
            "password": RABBITMQ_PASS,
            "vhost": RABBITMQ_VHOST
        }
    }