from typing import Dict
import logging
from ray import serve
from ray.serve import Application
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.requests import Request
from starlette.responses import StreamingResponse, Response, JSONResponse
from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.sampling_params import SamplingParams
from vllm.engine.async_llm_engine import AsyncLLMEngine
from http import HTTPStatus
import traceback
import json
import uuid

logger = logging.getLogger("ray.serve")
app = FastAPI()

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Enhanced handler for validation errors with detailed logging."""
    error_detail = str(exc)
    body = await request.body()
    logger.error(f"ValidationError: {error_detail}")
    logger.error(f"Request body: {body.decode('utf-8', errors='replace')}")
    logger.error(f"Request headers: {request.headers}")
    
    # Log specific validation errors
    for error in exc.errors():
        logger.error(f"Validation error: {error}")
    
    return create_error_response(HTTPStatus.BAD_REQUEST, f'Error parsing JSON payload: {error_detail}')

def create_error_response(status_code: HTTPStatus, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code.value, content={"detail": message})

@serve.deployment(name='VLLMInference', num_replicas=1, ray_actor_options={"num_gpus": 1.0})
@serve.ingress(app)
class VLLMGenerateDeployment:
    def __init__(self, **kwargs):
        """
        Construct a VLLM deployment.
        
        Args:
            model: name or path of the huggingface model to use
            quantization: method used to quantize the weights
            load_format: The format of the model weights to load.
            dtype: data type for model weights and activations.
            max_model_len: model context length.
            gpu_memory_utilization: the percentage of GPU memory to be used
        """
        logger.info(f"Initializing VLLMGenerateDeployment with kwargs: {kwargs}")
        
        # Filter out Ray-specific options
        engine_kwargs = kwargs.copy()
        if 'ray_actor_options' in engine_kwargs:
            del engine_kwargs['ray_actor_options']
        
        # Now create AsyncEngineArgs with the filtered kwargs
        try:
            args = AsyncEngineArgs(**engine_kwargs)
            logger.info(f"Engine args: {args}")
            
            self.engine = AsyncLLMEngine.from_engine_args(args)
            logger.info("AsyncLLMEngine initialized successfully")
            
            engine_model_config = self.engine.engine.get_model_config()
            self.tokenizer = self.engine.engine.tokenizer
            self.max_model_len = kwargs.get('max_model_len', engine_model_config.max_model_len)
            logger.info(f"Model max length: {self.max_model_len}")
            
            # Load model-specific configs
            try:
                from backend.rayserve_vllm.model_config import load_model_config
                logger.info(f"Loading model config for: {args.model}")
                self.model_config = load_model_config(args.model)
                logger.info(f"Model config loaded successfully: {self.model_config}")
            except Exception as e:
                logger.warn(f"No model config for: {args.model}")
                logger.warn(f"Exception: {str(e)}")
                self.model_config = None
                
        except Exception as e:
            logger.error(f"Failed to initialize AsyncLLMEngine: {str(e)}")
            logger.error(traceback.format_exc())
            raise

    def _next_request_id(self):
        return str(uuid.uuid1().hex)

    def _check_length(self, prompt: str, max_tokens=None) -> list:
        input_ids = self.tokenizer.encode(prompt)
        token_num = len(input_ids)
        
        if max_tokens is None:
            max_tokens = self.max_model_len - token_num
            
        if token_num + max_tokens > self.max_model_len:
            raise ValueError(
                f"This model's maximum context length is {self.max_model_len} tokens. "
                f"However, you requested {max_tokens + token_num} tokens "
                f"({token_num} in the messages, {max_tokens} in the completion). "
                f"Please reduce the length of the messages or completion."
            )
        return input_ids

    async def _stream_results(self, output_generator):
        num_returned = 0
        async for request_output in output_generator:
            output = request_output.outputs[0]
            text_output = output.text[num_returned:]
            response = {
                "output": text_output,
                "prompt_tokens": len(request_output.prompt_token_ids),
                "output_tokens": 1,
                "finish_reason": output.finish_reason
            }
            yield (json.dumps(response) + "\n").encode("utf-8")
            num_returned += len(text_output)

    async def _abort_request(self, request_id) -> None:
        await self.engine.abort(request_id)

    @app.get("/health")
    async def health(self) -> Response:
        """Health check."""
        return Response(status_code=200)

    @app.post("/generate")
    async def generate(self, request: Request) -> Response:
        """Generate completion for the request."""
        try:
            # Get raw request body
            body = await request.body()
            logger.info(f"Raw request body: {body.decode('utf-8', errors='replace')}")
            
            # Parse JSON
            try:
                json_data = json.loads(body)
                logger.info(f"Parsed JSON: {json_data}")
            except json.JSONDecodeError as e:
                logger.error(f"JSON decode error: {str(e)}")
                return create_error_response(HTTPStatus.BAD_REQUEST, f"Invalid JSON: {str(e)}")
            
            # Get required parameters
            prompt = json_data.get("prompt")
            messages = json_data.get("messages")
            stream = json_data.get("stream", False)
            max_tokens = json_data.get("max_tokens")
            
            if not prompt and not messages:
                return create_error_response(HTTPStatus.BAD_REQUEST, "Missing parameter 'prompt' or 'messages'")

            # Process messages if provided
            if not prompt and messages:
                if self.model_config:
                    logger.info(f"Using model config: {self.model_config}")
                    try:
                        prompt = self.model_config.prompt_format.generate_prompt(messages)
                        logger.info(f"Generated prompt: {prompt}")
                    except Exception as e:
                        logger.error(f"Error generating prompt: {str(e)}")
                        logger.error(traceback.format_exc())
                        return create_error_response(HTTPStatus.INTERNAL_SERVER_ERROR, f'Error generating prompt: {str(e)}')
                else:
                    logger.error(f"No model config found for messages: {messages}")
                    return create_error_response(HTTPStatus.BAD_REQUEST, 'Parameter "messages" requires a model config')

            # Check prompt length
            try:
                prompt_token_ids = self._check_length(prompt, max_tokens)
                logger.info(f"Prompt length check passed: {len(prompt_token_ids)} tokens")
            except ValueError as e:
                logger.error(f"Length check error: {str(e)}")
                raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(e))

            # Prepare sampling params - filter out non-sampling parameters
            sampling_params_keys = [
                "temperature", "top_p", "top_k", "max_tokens", "presence_penalty",
                "frequency_penalty", "stop", "ignore_eos", "logprobs", "best_of"
            ]
            sampling_params_dict = {
                k: v for k, v in json_data.items() 
                if k in sampling_params_keys and v is not None
            }
            
            logger.info(f"Sampling params: {sampling_params_dict}")
            sampling_params = SamplingParams(**sampling_params_dict)
            
            # Generate output
            request_id = self._next_request_id()
            logger.info(f"Request ID: {request_id}")

            output_generator = self.engine.generate(
                prompt=prompt,
                sampling_params=sampling_params,
                request_id=request_id
            )
            
            # Handle streaming vs non-streaming response
            if stream:
                background_tasks = BackgroundTasks()
                background_tasks.add_task(self._abort_request, request_id)
                return StreamingResponse(
                    self._stream_results(output_generator), 
                    background=background_tasks
                )
            else:
                # Non-streaming: get final output
                final_output = None
                async for request_output in output_generator:
                    if await request.is_disconnected():
                        await self.engine.abort(request_id)
                        return Response(status_code=200)
                    final_output = request_output

                if final_output:
                    text_outputs = final_output.outputs[0].text
                    prompt_tokens = len(final_output.prompt_token_ids)
                    output_tokens = len(final_output.outputs[0].token_ids)
                    finish_reason = final_output.outputs[0].finish_reason
                    
                    response = {
                        "output": text_outputs,
                        "prompt_tokens": prompt_tokens,
                        "output_tokens": output_tokens,
                        "finish_reason": finish_reason
                    }
                    return JSONResponse(content=response)
                else:
                    return create_error_response(
                        HTTPStatus.INTERNAL_SERVER_ERROR, 
                        "No output generated"
                    )

        except ValueError as e:
            raise HTTPException(status_code=HTTPStatus.BAD_REQUEST, detail=str(e))
        except Exception as e:
            logger.error('Unexpected error in generate()', exc_info=1)
            logger.error(traceback.format_exc())
            raise HTTPException(
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR, 
                detail=f'Server error: {str(e)}'
            )

def deployment(args: Dict) -> Application:
    """Create the Ray Serve deployment application."""
    # No need to modify args here - ray_actor_options are handled by the serve.deployment decorator
    return VLLMGenerateDeployment.bind(**args)