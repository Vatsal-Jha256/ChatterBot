
# Step 1: Send the request and get the correlation ID
CORR_ID=$(curl -s -X POST "http://localhost:8001/stream-chat" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: apitest1729" \
  -d '{"message":"Tell me a story","user_id":"user123","context":[],"stream":true}' | jq -r '.correlation_id')

echo "Correlation ID: $CORR_ID"

# Step 2: Check if CORR_ID is valid
if [[ -z "$CORR_ID" || "$CORR_ID" == "null" ]]; then
  echo "Error: Failed to retrieve a valid correlation ID."
  exit 1
fi

# Step 3: Stream and process the SSE output
curl -s -N -H "X-API-Key: apitest1729" "http://localhost:8001/stream/$CORR_ID" \
| while IFS= read -r line; do 
    if [[ "$line" == data:* ]]; then 
      # Remove the "data: " prefix
      token_json=$(echo "$line" | sed 's/^data: //')
      
      # Skip if token_json is exactly "[DONE]" or if it's not valid JSON
      if [[ "$token_json" == "[DONE]" ]]; then
        continue
      fi
      
      if echo "$token_json" | jq empty 2>/dev/null; then
        # Extract the token field (stringified JSON) and parse it for output
        inner_json=$(echo "$token_json" | jq -r '.token')
        if echo "$inner_json" | jq empty 2>/dev/null; then
          echo -n "$(echo "$inner_json" | jq -r '.output')"
        fi
      fi
    fi
  done

echo
