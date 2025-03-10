# api/routes/init.py
from fastapi import FastAPI

def register_routes(app: FastAPI):
    """Register all route modules with the FastAPI app"""
    from backend.core.api.routes.chat import router as chat_router
    from backend.core.api.routes.history import router as history_router
    from backend.core.api.routes.conversations import router as conversations_router
    from backend.core.api.routes.streaming import router as streaming_router
    from backend.core.api.routes.system import router as system_router
    
    app.include_router(chat_router)
    app.include_router(history_router)
    app.include_router(conversations_router)
    app.include_router(streaming_router)
    app.include_router(system_router)