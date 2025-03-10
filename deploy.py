import os
import argparse
import subprocess
import time
import sys
import logging
import requests
from pathlib import Path
import ray
from ray import serve

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

def start_redis():
    """Ensure Redis is running for context storage"""
    try:
        import redis
        r = redis.Redis(host='localhost', port=6379, db=0)
        r.ping()
        logger.info("Redis is already running")
    except Exception as e:
        logger.info(f"Redis check failed: {e}. Starting Redis")
        try:
            result = subprocess.run(
                ["docker", "ps", "-a", "--filter", "name=llm-redis", "--format", "{{.Names}}"],
                capture_output=True, text=True
            )
            if "llm-redis" in result.stdout:
                subprocess.run(["docker", "start", "llm-redis"], check=True)
            else:
                subprocess.run(
                    ["docker", "run", "-d", "-p", "6379:6379", "--name", "llm-redis", "redis"],
                    check=True
                )
            time.sleep(2)
            r = redis.Redis(host='localhost', port=6379, db=0)
            r.ping()
            logger.info("Redis started successfully")
        except Exception as e:
            logger.error(f"Could not start Redis: {e}")
            sys.exit(1)

def init_database():
    """Initialize database for long-term storage"""
    try:
        logger.info("Initializing database...")
        from backend.database.db_models import init_db
        init_db()
        logger.info("Database initialized")
    except Exception as e:
        logger.error(f"Database init failed: {e}")
        sys.exit(1)

def deploy_ray_serve(model_name, num_replicas=1, gpu_memory_util=0.9, max_model_len=4096):
    """
    Deploy the vLLM service using Ray Serve.
    This function initializes the Ray cluster if needed, deploys the service,
    and waits until the deployments are healthy.
    """
    try:
        # Initialize Ray if not already initialized.
        if not ray.is_initialized():
            ray.init(
                ignore_reinit_error=True,
                runtime_env={"working_dir": "."},
                num_cpus=4,
                num_gpus=1
            )
            logger.info("Ray local cluster initialized")

        # Import the deployment function.
        from backend.rayserve_vllm.vllm_serve import deployment

        deployment_args = {
            "model": model_name,
            "load_format": "auto",
            "dtype": "auto",
            "max_model_len": max_model_len,
            "gpu_memory_utilization": gpu_memory_util,
            "ray_actor_options": {"num_gpus": 1.0}
        }
        logger.info(f"Deploying Ray Serve with args: {deployment_args}")
        app = deployment(deployment_args)

        # Run the application.
        serve.run(app, name="vllm_service", route_prefix="/")

        # Wait for deployment(s) to be healthy.
        client = serve.context._connect()
        start_time = time.time()
        while True:
            if time.time() - start_time > 300:
                raise TimeoutError("Deployment timed out after 5 minutes")
            try:
                statuses = client.get_all_deployment_statuses()
                # Check that VLLMInference is healthy.
                inference_status = statuses.get("VLLMInference")
                # Optionally, if you have an additional router deployment, check it too.
                router_status = statuses.get("VLLMInferenceRouter")
                if (inference_status is not None and inference_status.status == "HEALTHY" and
                    (router_status is None or router_status.status == "HEALTHY")):
                    logger.info("Deployment healthy")
                    return True
                time.sleep(5)
            except Exception as e:
                logger.warning(f"Waiting for deployment: {str(e)}")
                time.sleep(5)

    except Exception as e:
        logger.error(f"Deployment failed: {e}", exc_info=True)
        return False

def start_api_server():
    """Start the FastAPI server"""
    try:
        logger.info("Starting API server")
        api_script = Path("backend/main.py").absolute()
        process = subprocess.Popen(
            [sys.executable, str(api_script)],
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )

        # Monitor server startup.
        for _ in range(30):
            if process.poll() is not None:
                break
            try:
                requests.get("http://localhost:8000/docs", timeout=1)
                logger.info("API server ready")
                return process
            except Exception:
                time.sleep(1)

        logger.error("API server failed to start")
        return False

    except Exception as e:
        logger.error(f"API server error: {e}")
        return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LLM Service Deployment")
    parser.add_argument("--model", default="asprenger/meta-llama-Llama-2-7b-chat-hf-gemm-w4-g128-awq")
    parser.add_argument("--replicas", type=int, default=1)
    parser.add_argument("--gpu-memory-util", type=float, default=0.9)
    parser.add_argument("--max-model-len", type=int, default=4096)
    parser.add_argument("--api-only", action="store_true")
    parser.add_argument("--ray-only", action="store_true")
    parser.add_argument("--init-db", action="store_true")
    args = parser.parse_args()

    start_redis()

    if args.init_db:
        init_database()

    ray_success = True
    if not args.api_only:
        ray_success = deploy_ray_serve(
            args.model,
            args.replicas,
            args.gpu_memory_util,
            args.max_model_len
        )

    api_process = None
    if not args.ray_only and ray_success:
        api_process = start_api_server()

    if ray_success and api_process:
        try:
            while True:
                time.sleep(10)
                if api_process.poll() is not None:
                    logger.error("API server died, restarting...")
                    api_process = start_api_server()
        except KeyboardInterrupt:
            logger.info("\nShutting down...")
            if api_process:
                api_process.terminate()
            if not args.api_only:
                serve.shutdown()
                ray.shutdown()
