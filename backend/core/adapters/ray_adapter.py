from typing import Dict, Any, List, Optional, AsyncGenerator, Union
import aiohttp
import logging
import json
from pydantic import BaseModel

from backend.core.models.api_models import APIRequest
from backend.rayserve_vllm.prompt_format import Message
from backend.core.config.settings import RAY_SERVE_URL

logger = logging.getLogger(__name__)

class RayServeAdapter:
    """
    Adapter for converting between FastAPI request formats and Ray Serve vLLM formats.
    Handles the transformation of request/response objects and communication with Ray Serve.
    """
    
    @staticmethod
    def api_request_to_generate_request(api_request: Union[APIRequest, Dict[str, Any]]) -> Dict[str, Any]:
        """
        Convert APIRequest to format expected by Ray Serve GenerateRequest
        
        Args:
            api_request (Union[APIRequest, Dict[str, Any]]): The API request from FastAPI
            
        Returns:
            Dict[str, Any]: Dictionary formatted for Ray Serve GenerateRequest
        """
        # Convert dict to APIRequest if needed
        if isinstance(api_request, dict):
            api_request = APIRequest(**api_request)
            
        # Convert context and message to Messages format
        messages: List[Dict[str, str]] = []
        
        # Add system context if available (first element in context might be system)
        if api_request.context and len(api_request.context) > 0 and api_request.context[0].startswith("System:"):
            # Extract system message without the "System:" prefix
            system_content = api_request.context[0][7:].strip()
            messages.append({"role": "system", "content": system_content})
            # Start with the second element (skip system message)
            context_items = api_request.context[1:]
        else:
            # Add default system message if none exists
            messages.append({"role": "system", "content": "You are a helpful assistant."})
            context_items = api_request.context or []
        
        # Add conversation context as alternating user/assistant messages
        for i, context_item in enumerate(context_items):
            role = "user" if i % 2 == 0 else "assistant"
            messages.append({"role": role, "content": context_item})
        
        # Add the current user message
        messages.append({"role": "user", "content": api_request.message})
        
        # Build the Ray Serve request
        generate_request = {
            "messages": messages,  # Don't convert to Message objects here - do it just before sending
            "max_tokens": api_request.max_tokens,
            "temperature": api_request.temperature,
            "stream": api_request.stream,
            # Add additional vLLM parameters if needed
            "top_p": api_request.top_p if hasattr(api_request, 'top_p') else 0.95,
            "top_k": api_request.top_k if hasattr(api_request, 'top_k') else 40,
        }
        
        logger.info(f"Converted messages count: {len(messages)}")
        
        return generate_request
    
    @staticmethod
    def generate_response_to_api_response(generate_response: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert Ray Serve GenerateResponse to API response format
        
        Args:
            generate_response (Dict[str, Any]): Response from Ray Serve
            
        Returns:
            Dict[str, Any]: Response formatted for API client
        """
        # Extract relevant data from generate_response
        # Handle vLLM's specific response format
        if "output" in generate_response:
            output = generate_response.get("output", "")
            prompt_tokens = generate_response.get("prompt_tokens", 0)
            output_tokens = generate_response.get("output_tokens", 0)
            finish_reason = generate_response.get("finish_reason", None)
        elif "choices" in generate_response:
            # Handle OpenAI-style response format that vLLM might use
            choices = generate_response.get("choices", [])
            if choices and len(choices) > 0:
                output = choices[0].get("text", "") or choices[0].get("message", {}).get("content", "")
                finish_reason = choices[0].get("finish_reason", None)
            else:
                output = ""
                finish_reason = None
            
            # Get token counts from usage if available
            usage = generate_response.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            output_tokens = usage.get("completion_tokens", 0)
        else:
            # Fallback for unexpected format
            output = generate_response.get("response", "")
            prompt_tokens = 0
            output_tokens = 0
            finish_reason = None
        
        # Format response for API
        api_response = {
            "response": output,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": prompt_tokens + output_tokens
            },
            "finish_reason": finish_reason
        }
        
        return api_response
    
    @staticmethod
    async def call_ray_serve(payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        Make non-streaming request to Ray Serve endpoint
        
        Args:
            payload (Dict[str, Any]): Request payload for Ray Serve
            
        Returns:
            Dict[str, Any]: Response from Ray Serve
        """
        try:
            logger.info(f"Sending request to Ray Serve: {payload}")
            
            # Handle messages if they are Pydantic objects
            if "messages" in payload and isinstance(payload["messages"], list) and len(payload["messages"]) > 0:
                if hasattr(payload["messages"][0], "dict"):
                    # Convert Pydantic objects to dicts
                    payload = {
                        **payload,
                        "messages": [msg.dict() if hasattr(msg, "dict") else msg for msg in payload["messages"]]
                    }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{RAY_SERVE_URL}/generate", json=payload, timeout=60) as response:
                    if response.status == 200:
                        result = await response.json()
                        logger.info(f"Received response from Ray Serve: {result}")
                        return result
                    else:
                        error_text = await response.text()
                        logger.error(f"Ray Serve error: {response.status}, {error_text}")
                        return {"response": f"Error communicating with LLM service: {response.status}"}
        except Exception as e:
            logger.error(f"Ray Serve call error: {e}")
            return {"response": f"Error communicating with LLM service: {str(e)}"}
    
    @staticmethod
    async def call_ray_serve_stream(payload: Dict[str, Any]) -> AsyncGenerator[bytes, None]:
        """
        Stream response from Ray Serve endpoint
        
        Args:
            payload (Dict[str, Any]): Request payload for Ray Serve
            
        Yields:
            bytes: Streaming response chunks from Ray Serve
        """
        try:
            # Ensure streaming is enabled
            payload["stream"] = True
            
            logger.info(f"Sending streaming request to Ray Serve: {payload}")
            
            # Handle messages if they are Pydantic objects
            if "messages" in payload and isinstance(payload["messages"], list) and len(payload["messages"]) > 0:
                if hasattr(payload["messages"][0], "dict"):
                    # Convert Pydantic objects to dicts
                    payload = {
                        **payload,
                        "messages": [msg.dict() if hasattr(msg, "dict") else msg for msg in payload["messages"]]
                    }
            
            # Set timeout for streaming request (higher for streaming)
            timeout = aiohttp.ClientTimeout(total=300)  # 5 minutes
            
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(f"{RAY_SERVE_URL}/generate", json=payload) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"Ray Serve streaming error: {response.status}, {error_text}")
                        yield json.dumps({
                            "token": "",
                            "error": f"Error communicating with LLM service: {response.status}",
                            "done": True
                        }).encode('utf-8')
                        return
                    
                    # Track full response for debug purposes
                    full_response = ""
                    
                    # Process each chunk from vLLM/Ray Serve
                    async for chunk in response.content:
                        try:
                            # Decode chunk
                            chunk_str = chunk.decode('utf-8').strip()
                            
                            # Skip empty chunks
                            if not chunk_str:
                                continue
                            
                            # Different vLLM versions might have different streaming formats
                            # Let's handle common patterns
                            
                            # Pattern 1: JSON with 'text' or 'content' field (OpenAI-style)
                            if chunk_str.startswith('{') and any(field in chunk_str for field in ['"text":', '"content":', '"delta":']):
                                try:
                                    chunk_data = json.loads(chunk_str)
                                    
                                    # Extract token from various possible formats
                                    token = ""
                                    done = False
                                    
                                    # OpenAI format with choices
                                    if "choices" in chunk_data and len(chunk_data["choices"]) > 0:
                                        choice = chunk_data["choices"][0]
                                        
                                        if "text" in choice:
                                            token = choice["text"]
                                        elif "delta" in choice and "content" in choice["delta"]:
                                            token = choice["delta"]["content"]
                                        elif "message" in choice and "content" in choice["message"]:
                                            token = choice["message"]["content"]
                                            
                                        done = choice.get("finish_reason") is not None
                                    
                                    # vLLM direct format
                                    elif "text" in chunk_data:
                                        token = chunk_data["text"]
                                        done = chunk_data.get("stop", False)
                                    elif "output" in chunk_data:
                                        token = chunk_data["output"]
                                        done = chunk_data.get("finish_reason") is not None
                                    
                                    # Update full response
                                    full_response += token
                                    
                                    # Yield in our expected format
                                    stream_chunk = {
                                        "token": token,
                                        "done": done
                                    }
                                    
                                    yield json.dumps(stream_chunk).encode('utf-8') + b'\n'
                                    
                                    if done:
                                        logger.info(f"Stream complete, received full text: {full_response[:100]}...")
                                        break
                                
                                except json.JSONDecodeError:
                                    # Not valid JSON, use as raw token
                                    full_response += chunk_str
                                    yield json.dumps({"token": chunk_str}).encode('utf-8') + b'\n'
                            
                            # Pattern 2: Plain text token
                            else:
                                full_response += chunk_str
                                yield json.dumps({"token": chunk_str}).encode('utf-8') + b'\n'
                                
                        except Exception as chunk_error:
                            logger.error(f"Error processing chunk: {chunk_error}")
                            yield json.dumps({"token": "", "error": str(chunk_error)}).encode('utf-8') + b'\n'
                    
                    # Final done message if we haven't sent one yet
                    yield json.dumps({"token": "", "done": True}).encode('utf-8') + b'\n'
                        
        except Exception as e:
            logger.error(f"Ray Serve streaming error: {e}")
            yield json.dumps({"token": "", "error": str(e), "done": True}).encode('utf-8') + b'\n'
    
    @staticmethod
    async def handle_request(api_request: APIRequest) -> Union[Dict[str, Any], AsyncGenerator[bytes, None]]:
        """
        Main interface to handle API requests to Ray Serve
        
        Args:
            api_request (APIRequest): The API request from FastAPI
            
        Returns:
            Dict[str, Any] or AsyncGenerator: Response from Ray Serve (streaming or non-streaming)
        """
        # Convert API request to Ray Serve format
        ray_payload = RayServeAdapter.api_request_to_generate_request(api_request)
        
        # Route based on streaming flag
        if api_request.stream:
            return RayServeAdapter.call_ray_serve_stream(ray_payload)
        else:
            # Get response and convert to API format
            generate_response = await RayServeAdapter.call_ray_serve(ray_payload)
            return RayServeAdapter.generate_response_to_api_response(generate_response)