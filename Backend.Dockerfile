# Base image: Python 3.9 image (can be adjusted based on your requirements)
FROM python:3.9-slim

# Set the working directory inside the container
WORKDIR /app

# Install system dependencies (including Docker and other utilities like Redis, etc.)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    gcc \
    g++ \
    make \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies for running the application
# Set up the working directory
WORKDIR /app

# Copy only the requirements file first to leverage Docker cache
COPY requirements.txt /app/

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the backend and deployment scripts to the container
COPY ./backend /app/backend

# Copy the unified deployment script to the container
COPY ./deploy.py /app/deploy.py

# Expose required ports (for example, 8001 for the FastAPI server)
EXPOSE 8001

# Set environment variables (optional: you can specify other variables needed for your app)
ENV API_PORT=8001
ENV API_HOST=0.0.0.0

# Run the deployment script (this will start the system with the appropriate parameters)
CMD ["python3", "deploy.py"]
