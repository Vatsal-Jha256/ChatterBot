import os
import torch
import logging
import json
import ray
from ray import serve
from typing import List, Dict, Any, Optional
from vllm import LLM, SamplingParams, AsyncLLMEngine, AsyncEngineArgs
from transformers import AutoTokenizer
from functools import lru_cache
import re
import argparse
import asyncio
import time
from fastapi.responses import StreamingResponse
import httpx

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@serve.deployment(
    ray_actor_options={"num_gpus": 1},
    autoscaling_config={"min_replicas": 1, "max_replicas": 3}
)
class RayLLMInference:
    """LLM inference service with Ray Serve integration - Optimized for memory usage"""
    
    def __init__(
        self, 
        model_name: str = "hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4",
        quantization: str = "awq",
        max_batch_size: int = 16,
        early_exit_threshold: float = 0.8,
    ):
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger("vllm_serve")
        self.logger.info(f"Initializing vLLM with {torch.cuda.device_count()} GPUs")
        self.early_exit_threshold = early_exit_threshold
        
        try:
            # Memory-optimized configuration
            engine_args = AsyncEngineArgs(
                model=model_name,
                quantization=quantization,
                tensor_parallel_size=torch.cuda.device_count(),
                gpu_memory_utilization=0.95,
                max_num_batched_tokens=4096,
                cpu_offload_gb=4,
                max_model_len=4096,
                enable_lora=False,
                enforce_eager=True
            )
            
            # Use AsyncLLMEngine and don't initialize separate LLM instance
            self.async_engine = AsyncLLMEngine.from_engine_args(engine_args)
            self.llm = self.async_engine.engine  # Get LLM from engine for sync operations
            
            self.tokenizer = AutoTokenizer.from_pretrained(model_name)
            self.tokenizer.add_special_tokens({'pad_token': '[PAD]'})
            self.model_name = model_name
            
            self.logger.info(f"Model {model_name} loaded successfully")
        except Exception as e:
            self.logger.error(f"Model loading error: {e}")
            raise

    def validate_prompt(self, prompt: str) -> str:
        """Sanitize the prompt by removing harmful or invalid characters."""
        prompt = prompt.replace('\x00', '')  # Remove null bytes
        prompt = re.sub(r'[^\w\s.,?!-]', '', prompt)[:2000]  # Basic sanitization
        return prompt.strip() or "Empty prompt"

    def format_chat_prompt(self, messages: List[dict]) -> str:
        """Format a chat prompt using the model's chat template."""
        return self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True
        )
    
    def format_chat_with_context(self, context: List[str], new_message: str) -> str:
        """Format a chat prompt with context history."""
        # Convert flat context list to message format
        messages = []
        
        # Add system message if not present
        if not context or not any(isinstance(c, dict) and c.get("role") == "system" for c in context):
            messages.append({"role": "system", "content": "You are a helpful assistant."})
            
        # Process context - assuming alternating user/assistant messages
        if context:
            is_context_dict = isinstance(context[0], dict)
            
            if is_context_dict:
                # Context is already in message format
                messages.extend(context)
            else:
                # Context is in alternating string format
                for i, msg in enumerate(context):
                    role = "user" if i % 2 == 0 else "assistant"
                    messages.append({"role": role, "content": msg})
                    
        # Add the new message
        messages.append({"role": "user", "content": new_message})
        
        return self.format_chat_prompt(messages)

    def generate(self, prompts: List[str], max_tokens: int = 256, temperature: float = 0.7) -> List[str]:
        """Generate responses synchronously for the given prompts."""
        sampling_params = SamplingParams(
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=0.95,
            repetition_penalty=1.1
        )
        try:
            outputs = self.llm.generate(prompts, sampling_params)
            responses = []
            for output in outputs:
                raw_text = output.outputs[0].text.strip()
                processed = self.postprocess_response(raw_text)
                if not processed:
                    processed = "I need more information to answer that properly."
                responses.append(processed)
            return responses
        except Exception as e:
            self.logger.error(f"Generation error: {e}")
            return ["I'm having trouble formulating a response right now."] * len(prompts)

    def postprocess_response(self, response: str) -> str:
        """Clean up the generated response by removing unwanted tokens and preserving proper formatting."""
        # Split on special tokens first
        response = response.split('eot_id')[0]
        
        # Remove header markers
        clean_response = re.sub(r'(start_header_id|end_header_id)\s*\w*\s*', '', response)
        
        # Remove any remaining template artifacts
        clean_response = re.sub(r'<\|(im_start|im_end|system|user|assistant)\|>', '', clean_response)
        clean_response = re.sub(r'\[/?INST\]', '', clean_response)
        clean_response = re.sub(r'\[/?SYS\]', '', clean_response)
        
        # Preserve spaces and newlines - don't collapse them excessively
        clean_response = re.sub(r'\n{3,}', '\n\n', clean_response)
        
        return clean_response.strip()

    async def generate_stream_token_by_token(self, prompt: str, max_tokens: int = 256, temperature: float = 0.7, request_id: str = None):
        """Generate streaming response token by token using AsyncLLMEngine."""
        if not request_id:
            request_id = f"req_{hash(prompt) % 10000}"
            
        sampling_params = SamplingParams(
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=0.95,
            repetition_penalty=1.1,
        )
        
        validated_prompt = self.validate_prompt(prompt)
        
        try:
            # Start the generation
            request_output = await self.async_engine.generate(validated_prompt, sampling_params, request_id)
            last_output_len = 0
            
            # Stream each token as it's generated
            while not request_output.finished:
                if request_output.outputs and len(request_output.outputs) > 0:
                    current_text = request_output.outputs[0].text
                    # Only yield the new token(s)
                    if len(current_text) > last_output_len:
                        new_text = current_text[last_output_len:]
                        last_output_len = len(current_text)
                        
                        # Clean the text chunk correctly
                        clean_text = new_text.replace("\x00", "")
                        
                        yield f"data: {json.dumps({'text': clean_text, 'done': False})}\n\n"
                        
                # Get the next token
                request_output = await self.async_engine.generate(validated_prompt, sampling_params, request_id, request_output)
                # Small delay to prevent CPU overload
                await asyncio.sleep(0.01)
            
            # Final response with done=True
            yield f"data: {json.dumps({'text': '', 'done': True})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            self.logger.error(f"Streaming generation error: {e}")
            yield f"data: {json.dumps({'text': '', 'error': str(e), 'done': True})}\n\n"
            yield "data: [DONE]\n\n"
    async def __call__(self, request):
        """Ray Serve endpoint handler with improved streaming"""
        try:
            # Extract request data - properly handle request.json
            if hasattr(request, 'json'):
                if callable(request.json):
                    data = await request.json()
                else:
                    data = request.json
            else:
                data = request  # Direct dictionary passed
                
            stream = data.get("stream", False)    
            
            # Handle different request structures
            if "message" in data and "context" in data:
                # Format with context/message API
                prompt = self.format_chat_with_context(
                    context=data.get("context", []),
                    new_message=data["message"]
                )
            elif "prompts" in data:
                # Direct prompts list
                return {"responses": self.generate(
                    [self.validate_prompt(p) for p in data["prompts"]],
                    max_tokens=data.get("max_tokens", 256),
                    temperature=data.get("temperature", 0.7)
                )}
            elif "messages" in data:
                # Standard chat format
                prompt = self.format_chat_prompt(data["messages"])
            else:
                # Single prompt
                prompt = data.get("prompt", "")
                
            # Handle streaming response
            if stream:
                # Use the correct method name: generate_stream_token_by_token
                async def stream_generator():
                    async for chunk in self.generate_stream_token_by_token(
                        prompt,
                        max_tokens=data.get("max_tokens", 256),
                        temperature=data.get("temperature", 0.7),
                        request_id=data.get("request_id", None)
                    ):
                        yield chunk
                        
                return StreamingResponse(
                    stream_generator(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                    }
                )
            else:
                # Non-streaming response
                responses = self.generate(
                    [self.validate_prompt(prompt)],
                    max_tokens=data.get("max_tokens", 256),
                    temperature=data.get("temperature", 0.7)
                )
                return {"response": responses[0]}
                
        except Exception as e:
            self.logger.error(f"Request handling error: {e}")
            return {"error": str(e), "response": "Error processing request"}

async def test_serve_streaming():
    """Test the streaming capability with proper token-by-token rendering"""
    import json
    import sys
    
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("test_serve")
    
    # Test data
    test_data = {
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Explain quantum computing in simple terms."}
        ],
        "stream": True,
        "max_tokens": 256,
        "temperature": 0.7
    }
    
    logger.info("Testing RayLLMInference streaming API...")
    
    # Wait for Ray Serve to be ready
    max_attempts = 5
    attempt = 0
    while attempt < max_attempts:
        try:
            health_check_url = "http://127.0.0.1:8000/-/healthz"
            async with httpx.AsyncClient() as client:
                logger.info(f"Attempting health check ({attempt+1}/{max_attempts})...")
                health_response = await client.get(health_check_url, timeout=5.0)
                logger.info(f"Ray Serve health check status: {health_response.status_code}")
                if health_response.status_code == 200:
                    logger.info("Ray Serve is healthy!")
                    break
            attempt += 1
            await asyncio.sleep(3)  # Wait before retrying
        except Exception as e:
            logger.warning(f"Health check attempt {attempt+1} failed: {str(e)}")
            attempt += 1
            await asyncio.sleep(3)  # Wait before retrying
    
    if attempt >= max_attempts:
        logger.error("Ray Serve health check failed after multiple attempts")
        logger.error("Make sure Ray Serve is running with 'serve.start(detached=True)' and the model is deployed")
        return
                
    # Now try the actual request with the correct endpoint URL
    try:
        async with httpx.AsyncClient() as client:
            logger.info("Sending request to streaming endpoint...")
            response = await client.post(
                "http://127.0.0.1:8000/RayLLMInference",
                json=test_data,
                timeout=60.0,
                headers={"Content-Type": "application/json"}
            )
            
            if response.status_code != 200:
                logger.error(f"Error status: {response.status_code}")
                logger.error(f"Response content: {response.text}")
                return
                
            logger.info("Streaming response started:")
            logger.info("="*50)
            
            full_text = ""
            sys.stdout.write("\033[?25l")  # Hide cursor
            try:
                async for line in response.aiter_lines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        data = line[6:]  # Remove "data: " prefix
                        try:
                            chunk_data = json.loads(data)
                            new_text = chunk_data.get("text", "")
                            full_text += new_text
                            
                            # Print each token as it arrives
                            sys.stdout.write(new_text)
                            sys.stdout.flush()
                            
                            # Small delay to simulate a more natural typing effect (optional)
                            await asyncio.sleep(0.01)
                            
                            if chunk_data.get("done", False):
                                logger.info("\nStream completed.")
                        except json.JSONDecodeError:
                            logger.warning(f"Failed to parse line: {line}")
            finally:
                sys.stdout.write("\033[?25h")  # Show cursor again
            
            logger.info("\n"+"="*50)
            logger.info("Final complete response:")
            logger.info(full_text)
            logger.info("="*50)
            
    except Exception as e:
        logger.error(f"Test failed with error: {str(e)}")

# Local testing function that uses the RayLLMInference class directly
async def test_local_streaming():
    """Test RayLLMInference streaming locally without deploying to Ray Serve"""
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("test_local")
    
    model_name = "hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4"
    logger.info(f"Testing local streaming with model: {model_name}")
    
    try:
        # Initialize Ray (needed when working with Ray deployments)
        ray.init(ignore_reinit_error=True)
        
        # Create a local instance of RayLLMInference WITHOUT using the deployment
        # Just instantiate the class directly
        inference_service = RayLLMInference(
            model_name=model_name,
            quantization="awq",
            max_batch_size=16,
            early_exit_threshold=0.8
        )
        
        # Test messages
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Explain quantum computing in simple terms."}
        ]
        
        # Format the prompt
        prompt = inference_service.format_chat_prompt(messages)
        
        logger.info("Generating streaming response:")
        logger.info("="*50)
        
        full_text = ""
        async for chunk in inference_service.generate_stream(
            prompt=prompt,
            max_tokens=256,
            temperature=0.7
        ):
            if chunk.startswith("data: ") and chunk != "data: [DONE]":
                data = chunk[6:]  # Remove "data: " prefix
                try:
                    chunk_data = json.loads(data)
                    new_text = chunk_data.get("text", "")
                    full_text += new_text
                    print(new_text, end="", flush=True)
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse chunk: {chunk}")
        
        logger.info("\n"+"="*50)
        logger.info("Final complete response:")
        logger.info(full_text)
        logger.info("="*50)
        
    except Exception as e:
        logger.error(f"Local test failed with error: {str(e)}")

def test_llm_directly(model_name, test_prompt, max_tokens=256, temperature=0.7):
    """Test the LLM directly without ray serve for local testing"""
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("llm_test")
    
    logger.info(f"Testing LLM with model: {model_name}")
    logger.info(f"Initializing model (this may take a moment)...")
    
    try:
        # Create a standalone LLM instance
        tensor_parallel_size = torch.cuda.device_count()
        logger.info(f"Detected {tensor_parallel_size} GPU(s)")
        
        llm = LLM(
            model=model_name,
            quantization="awq",
            tensor_parallel_size=tensor_parallel_size,
            cpu_offload_gb=2,
            gpu_memory_utilization=0.95,
            max_model_len=4096,
        )
        
        tokenizer = AutoTokenizer.from_pretrained(model_name)
        tokenizer.add_special_tokens({'pad_token': '[PAD]'})
        
        # Format the test message
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": test_prompt}
        ]
        formatted_prompt = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True
        )
        
        logger.info("Generating response...")
        # Generate response
        sampling_params = SamplingParams(
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=0.95,
            repetition_penalty=1.1
        )
        
        outputs = llm.generate([formatted_prompt], sampling_params)
        response = outputs[0].outputs[0].text.strip()
        
        # Clean up the response
        clean_response = re.sub(r'<[^>]+>', '', response)
        clean_response = re.sub(r'\[/?INST\]', '', clean_response)
        clean_response = re.sub(r'\[/?SYS\]', '', clean_response)
        clean_response = clean_response.strip()
        
        logger.info("="*50)
        logger.info(f"Prompt: {test_prompt}")
        logger.info("="*50)
        logger.info("Response:")
        logger.info(clean_response)
        logger.info("="*50)
        
        return clean_response
    except Exception as e:
        logger.error(f"Test failed: {e}")
        return f"Error: {str(e)}"

# Improved deployment function
def deploy_ray_serve(model_name="hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4", num_replicas=1):
    """Deploy the Ray Serve LLM service"""
    # Initialize Ray if not already initialized
    if not ray.is_initialized():
        ray.init(ignore_reinit_error=True)
    
    # Check for existing deployment and shut it down if needed
    try:
        serve.get_deployment("RayLLMInference")
        logger.info("Found existing deployment, shutting it down...")
        serve.shutdown()
        time.sleep(3)  # Give it time to shut down
    except:
        # No existing deployment found
        pass
    
    # Start Ray Serve
    serve.start(detached=True, http_options={"host": "0.0.0.0", "port": 8000})
    
    # Configure deployment options based on available resources
    num_gpus = ray.cluster_resources().get("GPU", 0)
    if num_gpus == 0:
        logger.warning("No GPUs detected. Model will run on CPU which will be very slow.")
    
    # Deploy with autoscaling
    deployment = RayLLMInference.options(
        name="RayLLMInference",
        ray_actor_options={"num_gpus": 1 if num_gpus > 0 else 0},
        autoscaling_config={
            "min_replicas": 1,
            "max_replicas": min(num_replicas, max(1, int(num_gpus))),
            "target_num_ongoing_requests_per_replica": 4
        }
    ).bind(model_name=model_name)

    # Run the deployment via Ray Serve
    serve.run(deployment)
    
    logger.info(f"Ray Serve deployment is ready at: http://localhost:8000/RayLLMInference")
    
    # Wait for deployment to be ready
    for i in range(10):
        try:
            logger.info(f"Checking if deployment is ready (attempt {i+1}/10)...")
            status = serve.status()
            logger.info(f"Deployment status: {status}")
            if "RayLLMInference" in str(status):
                logger.info("Deployment is ready!")
                break
            time.sleep(2)
        except Exception as e:
            logger.warning(f"Error checking deployment status: {e}")
            time.sleep(2)
    
    return deployment

# Entry point for CLI
async def main():
    parser = argparse.ArgumentParser(description="vLLM Serve: Deploy or test LLMs with Ray Serve")
    parser.add_argument("--deploy", action="store_true", help="Deploy the model with Ray Serve")
    parser.add_argument("--test", action="store_true", help="Test the model directly")
    parser.add_argument("--test-streaming", action="store_true", help="Test the model with streaming")
    parser.add_argument("--model", type=str, default="hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4",
                        help="Model to use for testing or deployment")
    parser.add_argument("--prompt", type=str, default="Explain quantum computing in simple terms.",
                        help="Test prompt to use when testing the model")
    parser.add_argument("--max-tokens", type=int, default=256, help="Maximum tokens to generate")
    parser.add_argument("--temperature", type=float, default=0.7, help="Temperature for sampling")
    parser.add_argument("--test-api", action="store_true", help="Test the deployed Ray Serve API")
    parser.add_argument("--test-local", action="store_true", help="Test streaming locally without deployment")
    parser.add_argument("--deploy-and-test", action="store_true", help="Deploy and immediately test (combined operation)")

    args = parser.parse_args()

    # Handle the combined deploy-and-test operation
    if args.deploy_and_test:
        logger.info("Deploying and testing in sequence...")
        deploy_ray_serve(args.model)
        # Wait for deployment to be fully ready
        logger.info("Waiting for deployment to be ready...")
        await asyncio.sleep(5)
        await test_serve_streaming()
        return

    # Only deploy if explicitly requested
    if args.deploy:
        deploy_ray_serve(args.model)
        logger.info("Deployment completed. Use --test-api to test the API.")

    if args.test:
        # Use the direct testing approach to avoid Ray Serve issues
        test_llm_directly(args.model, args.prompt, args.max_tokens, args.temperature)
    elif args.test_api:
        await test_serve_streaming()
    elif args.test_local:
        await test_local_streaming()
    elif not any([args.deploy, args.test, args.test_api, args.test_local, args.deploy_and_test]):
        # If no arguments are provided, show help
        parser.print_help()

if __name__ == "__main__":
    asyncio.run(main())