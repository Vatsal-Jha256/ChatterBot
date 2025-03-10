from typing import Dict, List, AsyncGenerator
import logging
import uuid
from http import HTTPStatus
from ray import serve
from ray.serve import Application
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.requests import Request
from starlette.responses import StreamingResponse, Response, JSONResponse
from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.sampling_params import SamplingParams
from vllm.engine.async_llm_engine import AsyncLLMEngine
from backend.rayserve_vllm.prompt_format import Message
from backend.rayserve_vllm.model_config import load_model_config
from backend.rayserve_vllm.protocol import GenerateRequest, GenerateResponse
# Add these imports at the top of your file
import traceback
import json
from fastapi.encoders import jsonable_encoder

logger = logging.getLogger("ray.serve")

app = FastAPI()

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Enhanced handler for validation errors with detailed logging."""
    # Log the error with full details
    error_detail = str(exc)
    body = await request.body()
    logger.error(f"ValidationError: {error_detail}")
    logger.error(f"Request body: {body.decode('utf-8', errors='replace')}")
    logger.error(f"Request headers: {request.headers}")
    
    # Log specific validation errors
    for error in exc.errors():
        logger.error(f"Validation error: {error}")
    
    return create_error_response(HTTPStatus.BAD_REQUEST, f'Error parsing JSON payload: {error_detail}')

def create_error_response(status_code: HTTPStatus,
                          message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code.value, content={"detail":message})

@serve.deployment(name='VLLMInference', 
                  num_replicas=1, 
                  ray_actor_options={"num_gpus": 1.0})
@serve.ingress(app)
class VLLMGenerateDeployment:
    def __init__(self, **kwargs):
        """
        Construct a VLLM deployment.

        Args:
            model: name or path of the huggingface model to use
            download_dir: directory to download and load the weights,
                default to the default cache dir of huggingface.
            load_format: The format of the model weights to load.
                "auto" will try to load the weights in the safetensors format and fall 
                back to the pytorch bin format if safetensors format is not available.
                "pt" will load the weights in the pytorch bin format.
                "safetensors" will load the weights in the safetensors format.
                "npcache" will load the weights in pytorch format and store a numpy 
                cache to speed up the loading.
                "dummy" will initialize the weights with random values, which is mainly 
                for profiling.
            dtype: data type for model weights and activations.
                The "auto" option will use FP16 precision
                for FP32 and FP16 models, and BF16 precision.
                for BF16 models.
            max_model_len: model context length. If unspecified, will be automatically 
                derived from the model.
            worker_use_ray: use Ray for distributed serving, will be
                automatically set when using more than 1 GPU
            pipeline_parallel_size: number of pipeline stages.
            tensor_parallel_size: number of tensor parallel replicas.
            block_size: token block size.
            swap_space: CPU swap space size (GiB) per GPU.
            gpu_memory_utilization: the percentage of GPU memory to be used for
                the model executor
            max_num_batched_tokens: maximum number of batched tokens per iteration
            max_num_seqs: maximum number of sequences per iteration.
            disable_log_stats: disable logging statistics.
            quantization: method used to quantize the weights
            engine_use_ray: use Ray to start the LLM engine in a separate
                process as the server process.
            disable_log_requests: disable logging requests.
        """
        logger.info(f"Initializing VLLMGenerateDeployment with kwargs: {kwargs}")
        args = AsyncEngineArgs(**kwargs)
        logger.info(f"Engine args: {args}")
        
        try:
            self.engine = AsyncLLMEngine.from_engine_args(args)
            logger.info("AsyncLLMEngine initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize AsyncLLMEngine: {str(e)}")
            logger.error(traceback.format_exc())
            raise
        
        engine_model_config = self.engine.engine.get_model_config()
        self.tokenizer = self.engine.engine.tokenizer
        self.max_model_len = kwargs.get('max_model_len', engine_model_config.max_model_len)
        logger.info(f"Model max length: {self.max_model_len}")

        try:
            logger.info(f"Loading model config for: {args.model}")
            self.model_config = load_model_config(args.model)
            logger.info(f"Model config loaded successfully: {self.model_config}")
        except FileNotFoundError as e:
            logger.warn(f"No model config for: {args.model}")
            logger.warn(f"Exception: {str(e)}")
            self.model_config = None

    def _next_request_id(self):
        return str(uuid.uuid1().hex)

    def _check_length(self, prompt:str, request:GenerateRequest) -> List[int]:
        input_ids = self.tokenizer.encode(prompt)
        
        token_num = len(input_ids)

        if request.max_tokens is None:
            request.max_tokens = self.max_model_len - token_num
        if token_num + request.max_tokens > self.max_model_len:
            raise ValueError(
            f"This model's maximum context length is {self.max_model_len} tokens. "
            f"However, you requested {request.max_tokens + token_num} tokens "
            f"({token_num} in the messages, "
            f"{request.max_tokens} in the completion). "
            f"Please reduce the length of the messages or completion.")
        return input_ids

    async def _stream_results(self, output_generator) -> AsyncGenerator[bytes, None]:
        num_returned = 0
        async for request_output in output_generator:
            output = request_output.outputs[0]
            text_output = output.text[num_returned:]
            response = GenerateResponse(output=text_output, 
                             prompt_tokens=len(request_output.prompt_token_ids), 
                             output_tokens=1, 
                             finish_reason=output.finish_reason)
            yield (response.json() + "\n").encode("utf-8")
            num_returned += len(text_output)

    async def _abort_request(self, request_id) -> None:
        await self.engine.abort(request_id)

    @app.get("/health")
    async def health(self) -> Response:
        """Health check."""
        return Response(status_code=200)

    @app.post("/generate")
    async def generate(self, request:Request) -> Response:
        """Generate completion for the request with enhanced logging.
        
        Args:
            request (Request): FastAPI request object
        
        Returns:
            Response: Response object
        """
        try:
            # First, log the raw request
            body = await request.body()
            logger.info(f"Raw request body: {body.decode('utf-8', errors='replace')}")
            
            # Try to parse the JSON
            try:
                json_data = json.loads(body)
                logger.info(f"Parsed JSON: {json_data}")
            except json.JSONDecodeError as e:
                logger.error(f"JSON decode error: {str(e)}")
                return create_error_response(HTTPStatus.BAD_REQUEST, f"Invalid JSON: {str(e)}")
            
            # Now try to validate with Pydantic
            try:
                validated_request = GenerateRequest(**json_data)
                logger.info(f"Validated request: {validated_request}")
            except Exception as e:
                logger.error(f"Pydantic validation error: {str(e)}")
                logger.error(traceback.format_exc())
                return create_error_response(HTTPStatus.BAD_REQUEST, f"Request validation error: {str(e)}")
            
            # Original logic starts here
            if not validated_request.prompt and not validated_request.messages:
                return create_error_response(HTTPStatus.BAD_REQUEST, "Missing parameter 'prompt' or 'messages'")

            if validated_request.prompt:
                prompt = validated_request.prompt
            else:
                if self.model_config:
                    logger.info(f"Using model config: {self.model_config}")
                    try:
                        prompt = self.model_config.prompt_format.generate_prompt(validated_request.messages)
                        logger.info(f"Generated prompt: {prompt}")
                    except Exception as e:
                        logger.error(f"Error generating prompt: {str(e)}")
                        logger.error(traceback.format_exc())
                        return create_error_response(HTTPStatus.INTERNAL_SERVER_ERROR, f'Error generating prompt: {str(e)}')
                else:
                    logger.error(f"No model config found for messages: {validated_request.messages}")
                    return create_error_response(HTTPStatus.BAD_REQUEST, 'Parameter "messages" requires a model config')

            try:
                prompt_token_ids = self._check_length(prompt, validated_request)
                logger.info(f"Prompt length check passed: {len(prompt_token_ids)} tokens")
            except ValueError as e:
                logger.error(f"Length check error: {str(e)}")
                raise HTTPException(HTTPStatus.BAD_REQUEST, str(e))

            request_dict = validated_request.dict(exclude=set(['prompt', 'messages', 'stream']))
            logger.info(f"Sampling params: {request_dict}")

            sampling_params = SamplingParams(**request_dict)
            request_id = self._next_request_id()
            logger.info(f"Request ID: {request_id}")

            output_generator = self.engine.generate(
                prompt=prompt,  # Pass the prompt text here
                sampling_params=sampling_params,
                request_id=request_id
            )
            if validated_request.stream:
                background_tasks = BackgroundTasks()
                # Abort the request processing in the engine if the socket connection drops
                background_tasks.add_task(self._abort_request, request_id)
                return StreamingResponse(self._stream_results(output_generator), 
                                        background=background_tasks)

            else:
                final_output = None
                async for request_output in output_generator:
                    if await request.is_disconnected():
                        await self.engine.abort(request_id)
                        return Response(status_code=200)
                    final_output = request_output

                text_outputs = final_output.outputs[0].text
                prompt_tokens = len(final_output.prompt_token_ids)
                output_tokens = len(final_output.outputs[0].token_ids)
                finish_reason = final_output.outputs[0].finish_reason
                return GenerateResponse(output=text_outputs, prompt_tokens=prompt_tokens, 
                                        output_tokens=output_tokens, finish_reason=finish_reason)

        except ValueError as e:
            raise HTTPException(HTTPStatus.BAD_REQUEST, str(e))
        except Exception as e:
            logger.error('Unexpected error in generate()', exc_info=1)
            logger.error(traceback.format_exc())
            raise HTTPException(HTTPStatus.INTERNAL_SERVER_ERROR, f'Server error: {str(e)}')

def deployment(args: Dict[str, str]) -> Application:
    return VLLMGenerateDeployment.bind(**args)