#!/usr/bin/env python3
"""
Unified Deployment Script for LLM Chatbot System
Manages Redis, Database, Ray Serve deployments, and FastAPI server
"""

import os
import sys
import time
import argparse
import subprocess
import logging
import requests
import redis
from redis.exceptions import ConnectionError
import ray
from ray import serve

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)
from dotenv import load_dotenv
#TODO: remove .vscode as well
# Load environment variables
load_dotenv()

# Service configuration defaults
DEFAULT_CONFIG = {
    "redis_container": "llm-redis",
    "redis_port": 6379,
    "api_port": 8001,
    "api_host": "0.0.0.0",
    "ray_init_cpus": 4,
    "ray_init_gpus": 1,
    "healthcheck_timeout": 300  # 5 minutes
}

def check_redis_connection(host="localhost", port=6379):
    """Check if Redis is available and return connection status."""
    try:
        r = redis.Redis(host=host, port=port, socket_connect_timeout=1)
        r.ping()
        return True
    except ConnectionError:
        return False

def manage_redis_service(action="start"):
    """Manage Redis service via Docker."""
    container_exists = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={DEFAULT_CONFIG['redis_container']}", "--format", "{{.Names}}"],
        capture_output=True, text=True
    ).stdout.strip()

    try:
        if action == "start":
            if check_redis_connection():
                logger.info("Redis is already running")
                return True

            if container_exists:
                logger.info("Starting existing Redis container")
                subprocess.run(
                    ["docker", "start", DEFAULT_CONFIG["redis_container"]],
                    check=True
                )
            else:
                logger.info("Creating new Redis container")
                subprocess.run([
                    "docker", "run", "-d",
                    "-p", f"{DEFAULT_CONFIG['redis_port']}:6379",
                    "--name", DEFAULT_CONFIG["redis_container"],
                    "redis:alpine"
                ], check=True)

            # Verify startup
            for _ in range(10):
                if check_redis_connection():
                    return True
                time.sleep(1)
            raise ConnectionError("Redis didn't start within 10 seconds")

        elif action == "stop":
            if container_exists:
                subprocess.run(
                    ["docker", "stop", DEFAULT_CONFIG["redis_container"]],
                    check=True
                )
                logger.info("Redis container stopped")
            return True

    except subprocess.CalledProcessError as e:
        logger.error(f"Redis management failed: {str(e)}")
        return False

def init_database():
    """Initialize the application database."""
    try:
        logger.info("Initializing database...")
        # Adjust imports according to your project structure
        from backend.database.db_models import init_db
        init_db()
        logger.info("Database initialized successfully")
        return True
    except Exception as e:
        logger.error(f"Database initialization failed: {str(e)}")
        return False

def deploy_ray_serve_model(model_config):
    """Deploy the LLM model using Ray Serve."""
    try:
        if not ray.is_initialized():
            ray.init(
                ignore_reinit_error=True,
                runtime_env={"working_dir": "."},
                num_cpus=DEFAULT_CONFIG["ray_init_cpus"],
                num_gpus=DEFAULT_CONFIG["ray_init_gpus"]
            )
            logger.info("Ray runtime initialized")

        # Import your deployment configuration
        from backend.rayserve_vllm.vllm_serve import deployment

        logger.info(f"Deploying model: {model_config['model']}")  # CORRECTED LINE
        app_handle = deployment(model_config)
        serve.run(app_handle, name="vllm_service", route_prefix="/")
        # Verify deployment health
        client = serve.context._connect()
        start_time = time.time()
        while time.time() - start_time < DEFAULT_CONFIG["healthcheck_timeout"]:
            statuses = client.get_all_deployment_statuses()
            # Check if any deployment matches our name and status
            healthy = any(
                status.name == "VLLMInference" and status.status == "HEALTHY"
                for status in statuses
            )
            if healthy:
                logger.info("Model deployment healthy")
                return True
            time.sleep(5)
        raise TimeoutError("Model deployment health check timed out")

    except Exception as e:
        logger.error(f"Ray Serve deployment failed: {str(e)}")
        return False

def manage_api_server(action="start"):
    """Manage the FastAPI server process."""
    api_process = None
    api_script = os.path.join(os.path.dirname(__file__), "backend", "main.py")

    try:
        if action == "start":
            logger.info("Starting API server")
            api_process = subprocess.Popen(
                [sys.executable, api_script],
                env=os.environ.copy(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )

            # Monitor startup
            start_time = time.time()
            while time.time() - start_time < DEFAULT_CONFIG["healthcheck_timeout"]:
                if api_process.poll() is not None:
                    break
                try:
                    response = requests.get(
                        f"http://{DEFAULT_CONFIG['api_host']}:{DEFAULT_CONFIG['api_port']}/docs",
                        timeout=1
                    )
                    if response.status_code == 200:
                        logger.info("API server ready")
                        return api_process
                except requests.ConnectionError:
                    time.sleep(1)
            
            if api_process.poll() is None:
                logger.error("API server health check timed out")
                api_process.terminate()
            return None

        elif action == "stop" and api_process:
            api_process.terminate()
            logger.info("API server stopped")

    except Exception as e:
        logger.error(f"API server management failed: {str(e)}")
        return None

def main():
    """Main deployment orchestration."""
    parser = argparse.ArgumentParser(description="LLM Chatbot Deployment Orchestrator")
    parser.add_argument("--model", required=True, help="HuggingFace model name or path")
    parser.add_argument("--quantization", choices=["awq", "gptq", None], default=None,
                        help="Quantization method")
    parser.add_argument("--max-model-len", type=int, default=4096,
                        help="Maximum model context length")
    parser.add_argument("--gpu-mem-util", type=float, default=0.9,
                        help="GPU memory utilization ratio")
    parser.add_argument("--init-db", action="store_true",
                        help="Initialize database before starting")
    parser.add_argument("--api-only", action="store_true",
                        help="Only start the API server")
    parser.add_argument("--ray-only", action="store_true",
                        help="Only deploy Ray Serve model")

    args = parser.parse_args()

    # 1. Service dependency management
    if not manage_redis_service("start"):
        sys.exit("Redis service management failed")

    # 2. Database initialization
    if args.init_db and not init_database():
        sys.exit("Database initialization failed")

    # 3. Model deployment
    ray_success = True
    if not args.api_only:
        model_config = {
            "model": args.model,
            "quantization": args.quantization,
            "max_model_len": args.max_model_len,
            "gpu_memory_utilization": args.gpu_mem_util
        }
        ray_success = deploy_ray_serve_model(model_config)

    # 4. API server management
    api_process = None
    if not args.ray_only and ray_success:
        api_process = manage_api_server("start")

    # 5. Process monitoring and cleanup
    try:
        while ray_success and api_process and api_process.poll() is None:
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("\nShutting down services...")
        if api_process:
            manage_api_server("stop")
        if not args.api_only:
            serve.shutdown()
            ray.shutdown()
        manage_redis_service("stop")
        logger.info("All services stopped")

if __name__ == "__main__":
    main()