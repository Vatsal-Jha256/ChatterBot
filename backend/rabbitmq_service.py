# rabbitmq_service.py
import os
import json
import logging
import asyncio
from typing import Dict, Any, Callable, Optional, Union
from functools import wraps
import uuid
import aio_pika
from aio_pika.abc import AbstractRobustConnection

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# RabbitMQ Configuration
RABBITMQ_HOST = os.getenv('RABBITMQ_HOST', 'localhost')
RABBITMQ_PORT = int(os.getenv('RABBITMQ_PORT', 5672))
RABBITMQ_USER = os.getenv('RABBITMQ_USER', 'guest')
RABBITMQ_PASS = os.getenv('RABBITMQ_PASS', 'guest')
RABBITMQ_VHOST = os.getenv('RABBITMQ_VHOST', '/')

# Queue names
CHAT_QUEUE = 'chat_requests'
RESPONSE_QUEUE = 'chat_responses'
BATCH_QUEUE = 'batch_tasks'

async def get_rabbitmq_connection() -> AbstractRobustConnection:
    """
    Get an async RabbitMQ connection.
    
    Returns:
        AbstractRobustConnection: An async RabbitMQ connection
    """
    try:
        connection = await aio_pika.connect_robust(
            host=RABBITMQ_HOST,
            port=RABBITMQ_PORT,
            login=RABBITMQ_USER,
            password=RABBITMQ_PASS,
            virtualhost=RABBITMQ_VHOST
        )
        return connection
    except Exception as e:
        logger.error(f"Error creating async RabbitMQ connection: {e}")
        raise

class RabbitMQPublisher:
    """Async Publisher using aio-pika"""
    
    def __init__(self):
        self.connection: Optional[AbstractRobustConnection] = None
        self.channel: Optional[aio_pika.Channel] = None
        self._lock = asyncio.Lock()
        self.exchanges = {}
        self._initialized = False
        
    async def initialize(self):
        """Initialize async connection"""
        if self._initialized and self.connection and not self.connection.is_closed:
            return

        try:
            if self.connection and not self.connection.is_closed:
                await self.connection.close()
                
            self.connection = await get_rabbitmq_connection()
            self.channel = await self.connection.channel()
            
            # Declare queues with priorities
            await self.channel.declare_queue(
                CHAT_QUEUE,
                durable=True,
                arguments={'x-max-priority': 10}
            )
            await self.channel.declare_queue(
                RESPONSE_QUEUE,
                durable=True,
                arguments={'x-max-priority': 10}
            )
            await self.channel.declare_queue(
                BATCH_QUEUE,
                durable=True,
                arguments={'x-max-priority': 10}
            )
            
            # Declare the stream exchange for token streaming
            self.stream_exchange = await self.channel.declare_exchange(
                "chat_streams_exchange",
                aio_pika.ExchangeType.TOPIC,
                durable=True
            )
            self.exchanges["chat_streams_exchange"] = self.stream_exchange
            
            self._initialized = True
            logger.info("RabbitMQ async publisher initialized")
        except Exception as e:
            self._initialized = False
            logger.error(f"RabbitMQ publisher init error: {e}")
            raise

    async def publish_message(self, queue_name: str, message: Dict[str, Any], 
                            correlation_id: Optional[str] = None, 
                            exchange: str = '', priority: int = 0) -> bool:
        """
        Publish a message to RabbitMQ
        
        Args:
            queue_name: The queue to publish to (used as routing key)
            message: Dictionary message to publish
            correlation_id: Optional ID for tracking the message
            exchange: Optional exchange name (defaults to default exchange)
            priority: Message priority (0-10)
            
        Returns:
            bool: True if successful, False otherwise
        """
        async with self._lock:
            try:
                if not self._initialized or not self.connection or self.connection.is_closed:
                    await self.initialize()
                    
                # Check if message is None
                if message is None or not isinstance(message, dict):
                    logger.error("Invalid message format")
                    return False
                    
                # Ensure channel is initialized
                if self.channel is None:
                    logger.error("Channel is None, cannot publish message")
                    await self.initialize()
                    if self.channel is None:
                        return False
                
                # Convert message to JSON string and encode
                try:
                    message_body = json.dumps(message).encode()
                except TypeError as e:
                    logger.error(f"Failed to JSON encode message: {e}")
                    return False
                
                if exchange == 'chat_streams_exchange':
                    target_exchange = self.exchanges.get('chat_streams_exchange')
                    if not target_exchange:
                        try:
                            target_exchange = await self.channel.declare_exchange(
                                "chat_streams_exchange",
                                aio_pika.ExchangeType.TOPIC,
                                durable=True
                            )
                            self.exchanges['chat_streams_exchange'] = target_exchange
                        except Exception as e:
                            logger.error(f"Failed to declare stream exchange: {e}")
                            return False
                            
                    await target_exchange.publish(
                        aio_pika.Message(
                            body=message_body,
                            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                            correlation_id=correlation_id,
                            priority=priority
                        ),
                        routing_key=queue_name  # Use queue_name as routing key, not correlation_id
                    )
                    return True
                    
                # Use the named exchange if specified, otherwise default exchange
                target_exchange = None
                if exchange and exchange != '':
                    if exchange not in self.exchanges:
                        try:
                            self.exchanges[exchange] = await self.channel.declare_exchange(
                                exchange, 
                                aio_pika.ExchangeType.TOPIC,
                                durable=True
                            )
                        except Exception as e:
                            logger.error(f"Failed to declare exchange {exchange}: {e}")
                            return False
                    target_exchange = self.exchanges.get(exchange)
                else:
                    # Make sure default_exchange exists
                    target_exchange = getattr(self.channel, 'default_exchange', None)
                
                # Check if we have a valid exchange
                if target_exchange is None:
                    logger.error(f"Target exchange is None. Exchange name: {exchange}")
                    # Try to use the default exchange directly if specified exchange is empty
                    if not exchange or exchange == '':
                        try:
                            # Publish directly to the queue without using an exchange
                            await self.channel.default_exchange.publish(
                                aio_pika.Message(
                                    body=message_body,
                                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                                    correlation_id=correlation_id,
                                    priority=priority
                                ),
                                routing_key=queue_name
                            )
                            return True
                        except Exception as direct_publish_error:
                            logger.error(f"Direct publish error: {direct_publish_error}")
                            return False
                    return False
                    
                # Now publish the message
                await target_exchange.publish(
                    aio_pika.Message(
                        body=message_body,
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                        correlation_id=correlation_id,
                        priority=priority
                    ),
                    routing_key=queue_name
                )
                return True
            except Exception as e:
                logger.error(f"Publish error: {e}")
                try:
                    await self.reconnect()
                except Exception as reconnect_error:
                    logger.error(f"Reconnect failed: {reconnect_error}")
                return False
    async def reconnect(self):
        """Reconnect handling"""
        self._initialized = False
        try:
            if self.connection and not self.connection.is_closed:
                await self.connection.close()
            await self.initialize()
        except Exception as e:
            logger.error(f"Reconnection failed: {e}")
            raise

    async def close(self):
        """Close connection"""
        if self.connection and not self.connection.is_closed:
            await self.connection.close()
            self._initialized = False

class RabbitMQConsumer:
    """Async Consumer using aio-pika"""
    
    def __init__(self, queue_name: str, callback: Callable):
        self.queue_name = queue_name
        self.callback = callback
        self.connection: Optional[AbstractRobustConnection] = None
        self.channel: Optional[aio_pika.Channel] = None
        self._initialized = False
        self._should_stop = False
        
    async def initialize(self) -> bool:
        """Initialize async consumer"""
        if self._initialized and self.connection and not self.connection.is_closed:
            return True
            
        try:
            if self.connection and not self.connection.is_closed:
                await self.connection.close()
                
            self.connection = await get_rabbitmq_connection()
            self.channel = await self.connection.channel()
            await self.channel.set_qos(prefetch_count=1)
            self._initialized = True
            logger.info(f"Async consumer initialized for {self.queue_name}")
            return True
        except Exception as e:
            self._initialized = False
            logger.error(f"Consumer init error: {e}")
            return False

    async def start_consuming(self):
        """Start async consumption"""
        self._should_stop = False
        
        while not self._should_stop:
            try:
                if not self._initialized or not self.connection or self.connection.is_closed:
                    if not await self.initialize():
                        await asyncio.sleep(5)  # Wait before retry
                        continue

                queue = await self.channel.declare_queue(
                    self.queue_name,
                    durable=True,
                    arguments={'x-max-priority': 10}
                )
                
                async with queue.iterator() as queue_iter:
                    async for message in queue_iter:
                        if self._should_stop:
                            break
                            
                        async with message.process():
                            try:
                                body = json.loads(message.body.decode())
                                
                                # Check if callback is async or sync
                                if asyncio.iscoroutinefunction(self.callback):
                                    # If async, await it directly
                                    await self.callback(body, message.correlation_id)
                                else:
                                    # If sync, run in thread pool
                                    loop = asyncio.get_running_loop()
                                    await loop.run_in_executor(
                                        None, 
                                        self.callback, 
                                        body, 
                                        message.correlation_id
                                    )
                            except Exception as e:
                                logger.error(f"Message processing failed: {e}")
                                # Don't requeue to avoid infinite loops
                                # But we could implement a dead letter queue here
            except Exception as e:
                logger.error(f"Consuming error: {e}")
                await self.reconnect()
                await asyncio.sleep(5)  # Wait before retry

    async def stop(self):
        """Stop consuming messages"""
        self._should_stop = True
        await self.close()

    async def reconnect(self):
        """Reconnect handling for consumer"""
        self._initialized = False
        try:
            if self.connection and not self.connection.is_closed:
                await self.connection.close()
            await self.initialize()
        except Exception as e:
            logger.error(f"Consumer reconnect failed: {e}")

    async def close(self):
        """Close consumer connection"""
        if self.connection and not self.connection.is_closed:
            await self.connection.close()
            self._initialized = False

# Decorator for async task processing
def async_task(queue_name=CHAT_QUEUE):
    """Decorator to send a function call to RabbitMQ for async processing"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                # Create task payload
                task_data = {
                    'function': func.__name__,
                    'args': args,
                    'kwargs': kwargs
                }
                
                # Publish to RabbitMQ asynchronously
                publisher = RabbitMQPublisher()
                await publisher.initialize()
                correlation_id = str(uuid.uuid4())
                result = await publisher.publish_message(
                    queue_name=queue_name,
                    message=task_data,
                    correlation_id=correlation_id
                )
                await publisher.close()
                
                return {
                    'success': result,
                    'message': 'Task submitted for async processing' if result else 'Failed to submit task',
                    'correlation_id': correlation_id
                }
            except Exception as e:
                logger.error(f"Async task submission error: {e}")
                return {
                    'success': False,
                    'message': f'Error submitting task: {str(e)}'
                }
        return wrapper
    return decorator

async def check_rabbitmq_connection():
    """Check if RabbitMQ is available"""
    try:
        connection = await aio_pika.connect_robust(
            host=RABBITMQ_HOST,
            port=RABBITMQ_PORT,
            virtual_host=RABBITMQ_VHOST,
            login=RABBITMQ_USER,
            password=RABBITMQ_PASS
        )
        await connection.close()
        logger.info("RabbitMQ is running")
        return True
    except Exception as e:
        logger.error(f"RabbitMQ connection check failed: {e}")
        return False

async def ensure_rabbitmq_running():
    """Ensure RabbitMQ is running, attempt to start with Docker if needed"""
    # Check if RabbitMQ is already running
    if await check_rabbitmq_connection():
        return True
        
    logger.info("Starting RabbitMQ in Docker")
    try:
        import subprocess
        process = await asyncio.create_subprocess_exec(
            "docker", "run", "-d", "-p", "5672:5672", "-p", "15672:15672", 
            "--name", "llm-rabbitmq", "rabbitmq:3-management",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode == 0:
            logger.info(f"RabbitMQ started in Docker: {stdout.decode().strip()}")
            # Wait for RabbitMQ to initialize
            await asyncio.sleep(10)
            return await check_rabbitmq_connection()
        else:
            logger.warning(f"Could not start RabbitMQ in Docker: {stderr.decode()}")
            return False
    except Exception as e:
        logger.warning(f"Could not start RabbitMQ in Docker: {e}")
        return False