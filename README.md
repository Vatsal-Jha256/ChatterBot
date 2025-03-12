# ChatterBot: Scalable LLM Chatbot System

## Overview

ChatterBot is a production-ready, scalable chat application powered by Large Language Models (LLMs) designed to support 10,000+ concurrent users. The system emphasizes scalability, reliability, and cost-effectiveness while maintaining a responsive user experience.

![ChatterBot Interface](images/demo.png)

## Executive Summary

This documentation outlines our production-ready, scalable chat application powered by Large Language Models (LLMs) designed to support 10,000+ concurrent users. The system emphasizes scalability, reliability, and cost-effectiveness while maintaining a responsive user experience.

Key architectural components include:
- FastAPI backend with horizontal scaling capabilities
- Ray Serve for efficient LLM inference with auto-scaling
- Redis for caching and context management
- RabbitMQ for asynchronous message processing
- PostgreSQL with database sharding for persistent storage
- React-based frontend for user interaction

The system design prioritizes decoupling components, stateless services, and asynchronous processing to ensure optimal performance under high load conditions.

## System Architecture

### Architecture Diagram

```mermaid
graph LR
A[User] --> B(Load Balancer / NGINX);
B --> C{FastAPI Server};
C --> D[Ray Serve Cluster];
C --> E[Redis Cluster];
C --> F[RabbitMQ];
B --> G[React Frontend];
D --> E;
F --> H[Worker Services];
H --> D;
C --> I[PostgreSQL Sharded Database];
I --> C;
```

### Alternative Cloud-Based Architecture

```
┌─────────────┐ ┌──────────────┐ ┌───────────────┐
│ Client      │─────►│ Next.js      │─────►│ FastAPI       │
└─────────────┘ │ (CDN Edge)    │ │ (ECS Fargate) │
                └──────────────┘ └───────┬───────┘
                                         │
                        ┌──────────▼──────────┐
                        │ SQS Queue           │
                        └──────────┬──────────┘
                                   │
         ┌───────────────▼───────────────┐
         │ vLLM Workers (Spot EC2)       │
         │ - AWQ Quantized Llama-2       │
         │ - PagedAttention KV Cache     │
         └───────────────┬───────────────┘
                         │
         ┌───────────────▼───────────────┐
         │ PostgreSQL (RDS)              │
         │ Redis (ElastiCache)           │
         └───────────────────────────────┘
```

## Component Breakdown

### FastAPI Backend (Horizontally Scalable)
- **Functionality:** Handles REST API endpoints, authentication, and rate limiting
- **Scaling Approach:** Stateless design behind load balancer allows horizontal scaling
- **Key Features:**
  - Redis connection pooling
  - RabbitMQ integration for asynchronous tasks
  - Middleware for auth and rate limiting
  - Support for both streaming and non-streaming responses

### Ray Serve Cluster
- **Functionality:** Manages LLM inference workloads
- **Scaling Approach:** Auto-scales based on request queue depth
- **Key Features:**
  - vLLM inference engine for efficient token generation
  - Multiple model replicas for parallel processing
  - GPU utilization optimization
  - Kubernetes-based auto-scaling

### Redis Cluster
- **Functionality:** Caches conversation context and handles rate limiting
- **Scaling Approach:** Sharded with read replicas
- **Key Features:**
  - Compressed message storage
  - LRU eviction policy for efficient memory usage
  - Distributed locking
  - High availability configuration

### RabbitMQ
- **Functionality:** Manages asynchronous task processing
- **Scaling Approach:** Auto-scaling consumer workers
- **Key Features:**
  - Priority queues for different request types
  - Dead letter queue processing
  - Message persistence for reliability
  - Configurable consumer prefetch

### PostgreSQL Sharded Database
- **Functionality:** Persistent storage for user data and conversation history
- **Scaling Approach:** Horizontal sharding by user ID
- **Key Features:**
  - Connection pooling
  - Read replicas for history endpoints
  - Time-series optimization for chat logs
  - Sharding implementation using user ID-based hash routing
  - Optimized indexes for conversation retrieval

## Database Implementation

The system implements a sharded database architecture for optimal scalability:

1. **Database Sharding Strategy**:
   - User-based horizontal sharding using consistent hashing
   - Each user is deterministically assigned to a specific shard
   - Automatic routing to appropriate database connections

2. **Schema Design**:
   - Users table with shard assignments
   - Conversations linked to users
   - Messages with conversation history and analytics data
   - Optimized indexes for query performance

3. **Connection Management**:
   - Dynamic connection pooling based on shard ID
   - Efficient session reuse
   - Automatic connection routing


## NGINX Configuration

A sample NGINX configuration is used for routing and load balancing:

## Error Handling and Retry Mechanisms

The system implements robust error handling with exponential backoff for retries:

## Streaming vs. Asynchronous Processing

The system supports both streaming and asynchronous processing modes:

1. **Streaming Mode:**
   - Implemented via Server-Sent Events (SSE)
   - Tokens are delivered to the frontend as they're generated
   - Provides immediate user feedback
   - API endpoints support streaming via `/stream/{correlation_id}`

2. **Asynchronous Batch Processing:**
   - Requests are queued and processed in the background
   - Results are polled via correlation IDs
   - Better handles high concurrency and traffic spikes

The API supports both modes, allowing clients to choose based on their requirements.

## Scalability Strategy

### Key Scaling Mechanisms

#### Horizontal Scaling
The system supports horizontal scaling at multiple layers:

1. **API Layer:**
   - Stateless FastAPI instances behind a load balancer
   - Configurable worker count per instance
   - Auto-scaling based on CPU/memory metrics

2. **Inference Layer:**
   - Ray Serve with dynamic scaling based on request queue
   - Configurable deployment using the following pattern:
   

3. **Database Layer:**
   - Sharded PostgreSQL for distributing write load
   - Read replicas for history and analytics queries
   - Connection pooling for efficient resource usage

#### Vertical Scaling
The system allows vertical scaling for specific components:

1. **LLM Inference:**
   - GPU instance type selection based on model size and throughput requirements
   - Memory optimization using quantization (AWQ 4-bit weights)

2. **Database:**
   - Instance size selection based on expected user volume
   - Buffer pool and work memory configuration

### Request Flow and Bottleneck Mitigation

#### Synchronous Chat Flow
1. User sends message through frontend
2. FastAPI server receives request and validates
3. Server accesses Redis to retrieve conversation context
4. Server sends inference request to Ray Serve
5. Ray Serve processes request and returns response
6. Server updates Redis cache and PostgreSQL database
7. Response returned to user

**Bottleneck Mitigation:**
- Redis connection pooling
- Database read replicas
- Ray Serve batch processing

#### Asynchronous Chat Flow
1. User sends message through frontend
2. FastAPI server receives request and validates
3. Server places task in RabbitMQ queue
4. Server returns correlation ID to user
5. Worker service processes queue message
6. Worker retrieves context from Redis
7. Worker calls Ray Serve for inference
8. Worker updates Redis and PostgreSQL
9. User polls for response with correlation ID

**Bottleneck Mitigation:**
- Queue prioritization
- Worker auto-scaling
- Asynchronous processing

#### Streaming Chat Flow
1. User initiates streaming chat
2. FastAPI creates SSE connection
3. Server places task in high-priority queue
4. Worker begins processing and calls streaming endpoint
5. Tokens streamed to user via SSE as they are generated
6. Final state saved to PostgreSQL after completion

**Bottleneck Mitigation:**
- Dedicated streaming queue
- Token-by-token processing
- Continuous batching in vLLM

## Reliability Features

### Failure Handling

#### Circuit Breaker Pattern
The system implements circuit breakers for critical service dependencies.

#### Request Retries
- Exponential backoff for retrying failed requests
- Configurable retry limits
- Dead letter queue for unprocessable messages

#### Graceful Degradation
- Fallback to lighter models during high load periods
- Reduced context window during capacity constraints
- Response caching for common queries

### High Availability Configuration

#### Redis
- Multi-node cluster with sentinel
- Persistence configuration
- Cross-AZ deployment

#### RabbitMQ
- Clustered deployment
- Message persistence
- Mirrored queues

#### Database
- Primary-replica architecture
- Automated failover
- Point-in-time recovery

### Monitoring and Alerting

#### Health Checks
- Service-level health endpoints
- Kubernetes liveness and readiness probes
- Dependency health monitoring

#### Metrics Collection
- Request latency tracking
- Queue depth monitoring
- Error rate tracking
- Resource utilization metrics

## Cost Optimization Strategy

### Infrastructure Efficiency

#### Compute Resources
- Spot instances for worker nodes
- Auto-scaling to match demand
- Right-sizing of instances

#### Model Optimization
- Quantization (AWQ) to reduce GPU memory requirements
- Model pruning for efficiency
- Continuous batching for higher throughput

### Caching Strategy

#### Response Caching
- Common query caching
- Partial context caching
- Result reuse for identical inputs

#### Context Management
- LRU eviction policy for conversation context
- Compressed storage format
- Tokenization caching

### Database Optimization

#### Access Patterns
- Read/write separation
- Query optimization
- Index strategy for conversation retrieval

#### Storage Efficiency
- Time-based partitioning for historical conversations
- Compression for older records
- Selective attribute storage

## API Endpoints

### System Endpoints

#### Health Check
**GET** `/health`

Check system health status

**Headers:**
- `X-API-Key: <your_api_key>`

**Response:**
```json
{
  "status": "healthy",
  "components": {
    "ray_serve": "up",
    "redis": "up",
    "database": "up",
    "rabbitmq": "up"
  },
  "timestamp": 1718901234.567
}
```

#### Queue Statistics
**GET** `/debug/queue-stats`

Get RabbitMQ queue metrics

**Headers:**
- `X-API-Key: <your_api_key>`

**Response:**
```json
{
  "chat_queue": {
    "message_count": 5,
    "consumer_count": 2
  },
  "response_queue": {
    "message_count": 3,
    "consumer_count": 1
  },
  "batch_queue": {
    "message_count": 0,
    "consumer_count": 0
  }
}
```

### Streaming Endpoints

#### Initiate Stream Chat
**POST** `/stream-chat`

Start a streaming chat session

**Headers:**
- `Content-Type: application/json`
- `X-API-Key: <your_api_key>`

**Request Body:**
```json
{
  "message": "Tell me about quantum physics",
  "user_id": "user123",
  "context": [],
  "stream": true
}
```

**Response:**
```json
{
  "status": "streaming",
  "message": "Connect to the streaming endpoint to receive tokens",
  "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
  "stream_endpoint": "/stream/550e8400-e29b-41d4-a716-446655440000"
}
```

#### Stream Responses
**GET** `/stream/{correlation_id}`

SSE endpoint for streaming responses

**Headers:**
- `X-API-Key: <your_api_key>`

**SSE Format:**
```
data: {"token": "Once", "done": false}
data: {"token": " upon", "done": false}
data: [DONE]
```

### Chat Endpoints

#### Synchronous Chat
**POST** `/chat`

Process chat request immediately

**Rate Limit:** 20 requests/minute

**Headers:**
- `Content-Type: application/json`
- `X-API-Key: <your_api_key>`

**Request Body:**
```json
{
  "message": "Explain relativity",
  "user_id": "user123",
  "context": [],
  "stream": false
}
```

**Response:**
```json
{
  "response": "**Relativity** refers to...",
  "conversation_id": 42,
  "processing_time": 1.234
}
```

#### Asynchronous Chat
**POST** `/async-chat`

Queue chat request for async processing

**Headers:**
- `Content-Type: application/json`
- `X-API-Key: <your_api_key>`

**Response:**
```json
{
  "status": "processing",
  "message": "Your request is being processed",
  "correlation_id": "550e8400-e29b-41d4-a716-446655440000",
  "check_endpoint": "/check-response/550e8400-e29b-41d4-a716-446655440000"
}
```

#### Check Async Response
**GET** `/check-response/{correlation_id}`

Check status of async request

**Headers:**
- `X-API-Key: <your_api_key>`

**Response:**
```json
{
  "response": "**Special relativity** states that...",
  "conversation_id": 42,
  "processing_time": 2.345
}
```

## Deployment Guide

### Prerequisites
- Docker and Docker Compose
- Python 3.8+
- Ray 2.0+
- Kubernetes cluster (optional for production)

### Quick Start

```bash
# 1. Install requirements
pip install -r requirements.txt

# 2. Deploy the LLM model with Ray Serve
./deploy.py --model asprenger/meta-llama-Llama-2-7b-chat-hf-gemm-w4-g128-awq --quantization awq --init-db

# 3. Start the worker service
python3 backend/rabbitmq/worker_service.py

# 4. Start the frontend
cd frontend
npm install
npm run dev
```


### Environment Configuration

**.env.example**
```ini
# Ray Configuration
RAY_ADDRESS=ray://localhost:10001
MODEL_NAME=asprenger/meta-llama-Llama-2-7b-chat-hf-gemm-w4-g128-awq
MAX_CONCURRENT=100

# Redis
REDIS_HOST=redis-cluster
REDIS_PORT=6379
REDIS_POOL_SIZE=50

# RabbitMQ
RABBITMQ_URI=amqp://user:pass@rabbitmq:5672/vhost
```

## Load Testing

### Testing Strategy
The system is tested using Locust with various scenarios:
1. **Streaming Test:** Focuses on streaming performance
2. **Concurrent User Test:** Simulates target of 10,000+ concurrent users
3. **Long-Running Test:** Checks stability over extended periods


### Running Load Tests

```bash
# Navigate to the tests directory
cd tests

# Start the Locust web interface
locust -f locustfile.py --host=http://localhost:8001
```

### Performance Benchmarks

Initial load testing with streaming showed that the system performed with low latency. However, some bottlenecks were observed. The architecture was tested on an NVIDIA 4060 with 8GB VRAM. Rate limiting caused a significant number of failures, despite the device being pushed to its limits. In a production environment, this issue could be effectively mitigated using multiple replicas and a load balancer.

## Technical Decisions

### vLLM Over Triton/TensorRT-LLM

The system uses vLLM for inference due to:
- 5-10x higher throughput with PagedAttention ([arXiv:2309.06180](https://arxiv.org/abs/2309.06180))
- Simpler deployment compared to NVIDIA-specific solutions
- Better support for open-source models

### AWQ Quantization

For model efficiency, AWQ 4-bit quantization is used:
- <1% accuracy drop vs FP16 ([arXiv:2306.00978](https://arxiv.org/abs/2306.00978))
- Compatible with vLLM's tensor parallelism
- Reduces GPU memory requirements by ~75%

### Hybrid Queue Architecture

The system uses a hybrid approach to message queuing:

#### Direct Mode
- For streaming chat interactions
- Uses FastAPI with WebSocket endpoints for low-latency responses
- Direct interaction with Ray Serve for token-by-token generation

#### Queue Mode
- For background tasks and load smoothing
- Uses RabbitMQ for asynchronous processing
- Helps manage load during traffic spikes

### Database Sharding Strategy

The PostgreSQL database is sharded by user ID for optimal performance:
- Even distribution of write load
- Locality of user data
- Ability to scale specific shards independently

## Future Enhancements

### 1. Comprehensive Monitoring with Prometheus & Grafana
* Implement Prometheus for metrics collection:
  * Request latency tracking
  * Queue depth monitoring
  * Error rate tracking
  * Resource utilization metrics
* Deploy Grafana for visualization dashboards:
  * Real-time system performance
  * Historical trends analysis
  * Custom alerting based on performance thresholds
  * User engagement metrics

### 2. Kubernetes Deployment for Advanced Auto-scaling
* Implement full Kubernetes orchestration:
  * Horizontal Pod Autoscaler (HPA) for automatic scaling based on CPU/memory usage
  * Custom metrics-based autoscaling for LLM inference
  * Node affinity for GPU workload optimization
  * StatefulSets for stateful components like databases
* Helm charts for simplified deployment:
  * Environment-specific configurations
  * Dependency management
  * Rollback capabilities

### 3. Advanced Load Balancing
* Implement NGINX or AWS Application Load Balancer:
  * SSL termination
  * Path-based routing
  * WebSocket support for streaming
  * Sticky sessions when needed
* Enhanced traffic management:
  * Circuit breaking for failed services
  * Request throttling
  * IP-based rate limiting
  * Geographic routing for multi-region deployments

### 4. Additional Enhancements
* Multi-model support
* Advanced analytics
* Custom model fine-tuning
* Enterprise integration options

## Conclusion

The proposed architecture delivers a scalable, reliable, and cost-effective LLM chatbot system capable of supporting 10,000+ users. Key strengths include:

1. **Scalability:** Horizontal scaling at all layers ensures the system can grow with user demand
2. **Reliability:** Circuit breakers, retries, and graceful degradation provide robust operation
3. **Performance:** Streaming architecture and optimized inference deliver responsive user experience
4. **Cost Efficiency:** Model quantization, caching, and resource optimization minimize operational costs

The system is ready for production deployment and can be extended with additional features as outlined in the Future Enhancements section.
