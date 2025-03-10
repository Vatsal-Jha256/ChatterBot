# utils/async_utils.py
import asyncio
from backend.database.db_service import get_db

async def process_db_operation(operation, *args, **kwargs):
    """Execute database operations asynchronously using a thread pool"""
    loop = asyncio.get_running_loop()
    
    def run_in_session():
        with get_db() as db:
            return operation(db, *args, **kwargs)
            
    # Run database operation in a thread pool
    return await loop.run_in_executor(None, run_in_session)