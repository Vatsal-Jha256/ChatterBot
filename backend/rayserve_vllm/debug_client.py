import argparse
import json
import requests
import logging

logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("vllm-debug")

def test_endpoint(url, payload, headers=None):
    """Test an endpoint with a payload and print full details of request/response."""
    if headers is None:
        headers = {'Content-Type': 'application/json'}
    
    logger.info(f"Sending request to: {url}")
    logger.info(f"Headers: {headers}")
    logger.info(f"Payload: {payload}")
    
    try:
        # Convert payload to string for display
        payload_str = json.dumps(payload, indent=2)
        logger.info(f"JSON payload (formatted):\n{payload_str}")
        
        # Send the request
        response = requests.post(url, json=payload, headers=headers)
        
        # Print the response details
        logger.info(f"Response status code: {response.status_code}")
        logger.info(f"Response headers: {response.headers}")
        
        try:
            response_json = response.json()
            logger.info(f"Response body (JSON): {json.dumps(response_json, indent=2)}")
        except:
            logger.info(f"Response body (text): {response.text}")
    
    except Exception as e:
        logger.error(f"Error during request: {str(e)}", exc_info=True)

def main():
    parser = argparse.ArgumentParser(description="Test VLLM API endpoints with detailed logging")
    parser.add_argument("--url", default="http://localhost:8000/generate", 
                      help="URL of the endpoint to test")
    parser.add_argument("--test", choices=["prompt", "messages", "minimal", "all"], 
                      default="all", help="Type of test to run")
    
    args = parser.parse_args()
    
    if args.test in ["prompt", "all"]:
        # Test with prompt only
        payload = {
            "prompt": "What is the capital of France?",
            "stream": False,
            "max_tokens": 128
        }
        logger.info("=== Testing with prompt only ===")
        test_endpoint(args.url, payload)
    
    if args.test in ["messages", "all"]:
        # Test with messages
        payload = {
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "What is the capital of France?"}
            ],
            "stream": False,
            "max_tokens": 128
        }
        logger.info("=== Testing with messages ===")
        test_endpoint(args.url, payload)
    
    if args.test in ["minimal", "all"]:
        # Test with minimal payload
        payload = {
            "prompt": "Hello",
            "max_tokens": 10
        }
        logger.info("=== Testing with minimal payload ===")
        test_endpoint(args.url, payload)

if __name__ == "__main__":
    main()