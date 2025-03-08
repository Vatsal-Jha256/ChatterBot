import os
import argparse
import ray
from ray import serve
import logging
import subprocess
import time
import sys

#TODO: have to add launching of worker service and rabbitmq flexibly here only

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

logger = logging.getLogger(__name__)

from dotenv import load_dotenv
load_dotenv()  # Load environment variables from .env file

def start_redis():
    """Ensure Redis is running for context storage"""
    try:
        # Check if Redis is available
        import redis
        r = redis.Redis(host='localhost', port=6379, db=0)
        r.ping()
        logger.info("Redis is already running")
    except:
        logger.info("Starting Redis in background")
        try:
            # Try to start Redis with Docker
            subprocess.run(
                ["docker", "run", "-d", "-p", "6379:6379", "--name", "llm-redis", "redis"],
                check=True
            )
            logger.info("Redis started in Docker")
        except:
            logger.warning("Could not start Redis in Docker, please ensure Redis is available")

def init_database():
    """Initialize database for long-term storage"""
    try:
        logger.info("Initializing database...")
        from db_models import init_db
        init_db()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization error: {e}")

def deploy_ray_serve(model_name, num_replicas=1):
    """Deploy the Ray Serve LLM service"""
    from vllm_serve import RayLLMInference
    
    # Initialize Ray
    if not ray.is_initialized():
        ray.init(ignore_reinit_error=True)
    
    # Start Ray Serve
    serve.start(detached=True)
    
    # Configure deployment options based on available resources
    num_gpus = ray.cluster_resources().get("GPU", 0)
    if num_gpus == 0:
        logger.warning("No GPUs detected. Model will run on CPU which will be very slow.")
    
    # Deploy with autoscaling - fixed syntax for newer Ray Serve
    # Create the deployment object with the desired options and bind the model_name argument.
    deployment = RayLLMInference.options(
        name="RayLLMInference",
        ray_actor_options={"num_gpus": 1 if num_gpus > 0 else 0},
        autoscaling_config={
            "min_replicas": 1,
            "max_replicas": min(num_replicas, max(1, int(num_gpus))),
            "target_num_ongoing_requests_per_replica": 4
        }
    ).bind(model_name=model_name)

    # Run the deployment via Ray Serve.
    serve.run(deployment)
    
    logger.info(f"Ray Serve deployment is ready at: http://localhost:8000/RayLLMInference")
    return deployment

def start_api_server():
    """Start the FastAPI server for the front-end"""
    logger.info("Starting API server")
    env = os.environ.copy()
    env["RAY_SERVE_URL"] = "http://localhost:8000/RayLLMInference"
    subprocess.Popen([sys.executable, "api_server.py"], env=env)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deploy LLM service with Ray Serve")
    parser.add_argument(
        "--model", 
        default="hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4",
        help="Model to deploy"
    )
    parser.add_argument(
        "--replicas", 
        type=int, 
        default=1,
        help="Maximum number of model replicas"
    )
    parser.add_argument(
        "--api-only", 
        action="store_true",
        help="Only start API server (assumes Ray is already running)"
    )
    parser.add_argument(
        "--ray-only", 
        action="store_true",
        help="Only deploy Ray Serve (don't start API server)"
    )
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="Initialize the database schema"
    )
    
    args = parser.parse_args()
    
    # Start Redis for context storage
    start_redis()
    
    # Initialize database if requested
    if args.init_db:
        init_database()
    
    # Deploy Ray Serve if requested
    if not args.api_only:
        deployment = deploy_ray_serve(args.model, args.replicas)
        logger.info("Ray Serve deployment successful: RayLLMInference")    
    
    # Start API server if requested
    if not args.ray_only:
        start_api_server()
        logger.info("API server started")
        
    if not args.api_only and not args.ray_only:
        logger.info("Full system deployment completed")
        
    # Keep main process running
    if not args.api_only:
        try:
            while True:
                time.sleep(10)
        except KeyboardInterrupt:
            logger.info("Shutting down...")