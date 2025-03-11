from locust import HttpUser, task, between
import json

class StreamChatUser(HttpUser):
    wait_time = between(1, 3)  # Wait time between requests (random between 1-3s)

    @task
    def send_message(self):
        # Step 1: Send the initial request and get the correlation ID
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": "apitest1729"
        }
        data = {"message": "Tell me a story", "user_id": "user123", "context": [], "stream": True}
        
        response = self.client.post("/stream-chat", headers=headers, json=data)
        if response.status_code == 200:
            correlation_id = response.json().get("correlation_id")

            if correlation_id:
                # Step 2: Stream SSE response
                self.client.get(f"/stream/{correlation_id}", headers=headers, stream=True)

