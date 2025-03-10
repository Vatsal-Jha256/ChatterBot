from typing import List, Optional
from pydantic import BaseModel
from backend.rayserve_vllm.prompt_format import Message
import logging
from pydantic import BaseModel, validator, root_validator
logger = logging.getLogger("ray.serve")
class GenerateRequest(BaseModel):
    """Generate completion request.

        prompt: Prompt to use for the generation
        messages: List of messages to use for the generation
        stream: Bool flag whether to stream the output or not
        max_tokens: Maximum number of tokens to generate per output sequence.
        temperature: Float that controls the randomness of the sampling. Lower
            values make the model more deterministic, while higher values make
            the model more random. Zero means greedy sampling.
        ignore_eos: Whether to ignore the EOS token and continue generating
            tokens after the EOS token is generated.
    
        Note that vLLM supports many more sampling parameters that are ignored here.
        See: vllm/sampling_params.py in the vLLM repository.
        """
    prompt: Optional[str] = None
    messages: Optional[List[Message]] = None
    stream: Optional[bool] = False
    max_tokens: Optional[int] = 128
    temperature: Optional[float] = 0.7
    ignore_eos: Optional[bool] = False
    
    @root_validator(pre=True)
    def validate_input(cls, values):
        """Validate that either prompt or messages is provided."""
        logger.info(f"Validating input values: {values}")
        prompt = values.get('prompt')
        messages = values.get('messages')
        
        if prompt is None and messages is None:
            logger.error("Neither 'prompt' nor 'messages' provided")
        
        # Log messages structure if present
        if messages is not None:
            logger.info(f"Messages type: {type(messages)}")
            if isinstance(messages, list):
                for i, msg in enumerate(messages):
                    logger.info(f"Message {i}: {msg}")
            else:
                logger.error(f"'messages' should be a list, got {type(messages)}")
        
        return values
    
    @validator('max_tokens')
    def validate_max_tokens(cls, v):
        if v is not None and v <= 0:
            logger.error(f"max_tokens must be positive, got {v}")
            raise ValueError(f"max_tokens must be positive, got {v}")
        return v
        
    @validator('temperature')
    def validate_temperature(cls, v):
        if v is not None and (v < 0 or v > 2):
            logger.warning(f"temperature should typically be between 0 and 2, got {v}")
        return v

class GenerateResponse(BaseModel):
    """Generate completion response.

        output: Model output
        prompt_tokens: Number of tokens in the prompt
        output_tokens: Number of generated tokens
        finish_reason: Reason the genertion has finished
    """
    output: str
    prompt_tokens: int
    output_tokens: int
    finish_reason: Optional[str]
