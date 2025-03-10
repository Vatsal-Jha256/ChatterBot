from typing import Dict, Any, Optional, Union, AsyncGenerator
import logging
import json
from backend.core.models.api_models import APIRequest
from backend.core.adapters.ray_adapter import RayServeAdapter

logger = logging.getLogger(__name__)

async def handle_ray_request(payload: Dict[str, Any]) -> Union[Dict[str, Any], AsyncGenerator[bytes, None]]:
    """
    Handle request to Ray Serve for inference
    
    Args:
        payload (Dict[str, Any]): Request payload with context, message, and parameters
        
    Returns:
        Union[Dict[str, Any], AsyncGenerator[bytes, None]]: Response from Ray Serve (streaming or non-streaming)
    """
    try:
        logger.info(f"Handling ray request with payload keys: {payload.keys()}")
        
        # Determine if this is an API format request or direct ray format
        if 'message' in payload and ('context' in payload or 'user_id' in payload):
            # This is an API format request
            logger.info("Processing API format request")
            
            # Convert from API format to Ray format if needed
            if not isinstance(payload, APIRequest):
                # Create APIRequest object if payload is a dict
                api_request = APIRequest(**payload)
            else:
                api_request = payload
                
            # Use adapter to convert from API format to Ray format
            ray_payload = RayServeAdapter.api_request_to_generate_request(api_request)
            logger.info(f"Converted API request to Ray format with keys: {ray_payload.keys()}")
            
            # Extract streaming flag
            streaming = payload.get('stream', False)
        else:
            # This is already in Ray format
            logger.info("Using payload directly for Ray request")
            ray_payload = payload
            
            # Extract streaming flag
            streaming = payload.get('stream', False)
        
        # Handle streaming vs non-streaming
        if streaming:
            logger.info("Using streaming mode for Ray request")
            
            # Make sure the streaming flag is set in the payload
            ray_payload['stream'] = True
            
            # Return streaming generator
            return await RayServeAdapter.call_ray_serve_stream(ray_payload)
        else:
            # Get non-streaming response from Ray Serve
            logger.info("Using non-streaming mode for Ray request")
            
            # Make sure the streaming flag is not set
            ray_payload['stream'] = False
            
            # Get response
            generate_response = await RayServeAdapter.call_ray_serve(ray_payload)
            
            # Convert response back to API format if needed
            if 'output' in generate_response or 'choices' in generate_response:
                api_response = RayServeAdapter.generate_response_to_api_response(generate_response)
                logger.info(f"Converted Ray response to API format with keys: {api_response.keys()}")
                return api_response
            else:
                # Response is already in API format
                logger.info(f"Using Ray response directly with keys: {generate_response.keys()}")
                return generate_response
    except Exception as e:
        logger.error(f"Error handling Ray request: {e}", exc_info=True)
        return {"response": f"Error processing request: {str(e)}"}

# Helper for direct streaming without conversion
async def handle_ray_streaming(payload: Dict[str, Any]) -> AsyncGenerator[bytes, None]:
    """
    Handle streaming request to Ray Serve with minimal processing
    
    Args:
        payload (Dict[str, Any]): Direct Ray payload
        
    Returns:
        AsyncGenerator[bytes, None]: Raw streaming response
    """
    try:
        # Ensure streaming is enabled
        payload['stream'] = True
        
        # Pass directly to ray adapter for streaming
        async for chunk in await RayServeAdapter.call_ray_serve_stream(payload):
            yield chunk
    except Exception as e:
        logger.error(f"Direct streaming error: {e}")
        yield json.dumps({"token": "", "error": str(e), "done": True}).encode('utf-8')