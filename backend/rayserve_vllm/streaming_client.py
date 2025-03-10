import os
import sys
import logging
import time
import argparse
import requests
import json
import traceback

# Configure logging
logging.basicConfig(level=logging.DEBUG, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("streaming-client")


def _get_result(response: requests.Response):
    try:
        result = response.json()
        return result
    except requests.JSONDecodeError as e:
        logger.error(f"Failed to decode JSON response: {e}")
        logger.error(f"Response text: {response.text}")
        raise RuntimeError(
            f"Error decoding JSON from {response.url}. Text response: {response.text}",
            response=response,
        ) from e

def main(host, port, stream, user_message, max_tokens, temperature, system_message):
    url = f'http://{host}:{port}/generate'
    headers = {'Content-Type': 'application/json'}
    
    # Create payload
    payload = {
        "messages": [
            {"role": "system", "content": system_message}, 
            {"role": "user", "content": user_message}
        ], 
        "stream": stream, 
        "max_tokens": max_tokens, 
        "temperature": temperature
    }
    
    # Log the payload and URL
    logger.debug(f"URL: {url}")
    logger.debug(f"Headers: {headers}")
    
    # Log the payload as a formatted JSON string
    payload_str = json.dumps(payload, indent=2)
    logger.debug(f"Payload (formatted JSON):\n{payload_str}")
    
    # Also try an alternative payload with just the prompt
    alt_payload = {
        "prompt": f"System: {system_message}\nUser: {user_message}",
        "stream": stream,
        "max_tokens": max_tokens,
        "temperature": temperature
    }
    
    try:
        # First try with the messages format
        logger.info("Attempting request with messages format...")
        response = requests.post(url, headers=headers, json=payload, 
                              stream=stream)
        
        # Log the response status and headers
        logger.debug(f"Response status: {response.status_code}")
        logger.debug(f"Response headers: {response.headers}")
        
        if response.status_code != 200:
            logger.warning(f"Request failed with status {response.status_code}")
            logger.warning(f"Response: {response.text}")
            
            # If messages format fails, try with prompt format
            logger.info("Attempting alternative request with prompt format...")
            alt_response = requests.post(url, headers=headers, json=alt_payload, 
                                     stream=stream)
            
            logger.debug(f"Alt response status: {alt_response.status_code}")
            if alt_response.status_code == 200:
                logger.info("Alternative request succeeded!")
                response = alt_response
            else:
                logger.warning(f"Alternative request also failed: {alt_response.text}")
        
        # Process the successful response
        if response.status_code == 200:
            logger.info("Request succeeded!")
            if stream:
                with os.fdopen(sys.stdout.fileno(), "wb", closefd=False) as stdout:
                    start = time.perf_counter()
                    num_tokens = 0
                    for line in response.iter_lines():
                        try:
                            text = json.loads(line.decode("utf-8"))['output']
                            stdout.write(text.encode("utf-8"))
                            stdout.flush()
                            num_tokens += 1
                        except Exception as e:
                            logger.error(f"Error processing streaming response: {e}")
                            logger.error(f"Raw line: {line}")
                    
                    duration_s = time.perf_counter() - start
                    print(f"\n{num_tokens / duration_s:.2f} token/s")
            else:
                result = _get_result(response)
                print(json.dumps(result, indent=2))
        else:
            print(f'HTTP status code: {response.status_code}')
            print(response.text)
                    
    except Exception as e:
        logger.error(f"Error: {e}")
        logger.error(traceback.format_exc())
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--system-message", type=str, default="You are a helpful and truthful assistant.")
    parser.add_argument("--user-message", type=str, default="What can I do on a weekend trip to London?")
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument('--no-stream', dest='stream', action='store_false')
    parser.add_argument('--prompt-only', action='store_true', help='Use prompt instead of messages')
    
    args = parser.parse_args()
    main(host=args.host, port=args.port, stream=args.stream, 
         user_message=args.user_message, max_tokens=args.max_tokens, 
         temperature=args.temperature, system_message=args.system_message)