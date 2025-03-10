# models/api_models.py
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional

class APIRequest(BaseModel):
    user_id: str = Field(..., min_length=3, max_length=50)
    message: str = Field(..., min_length=1, max_length=2000)
    context: Optional[List[str]] = Field(default_factory=list, max_length=10)
    max_tokens: int = Field(default=256, ge=10, le=1024)
    temperature: float = Field(default=0.7, ge=0.1, le=1.0)
    conversation_id: Optional[str] = None
    stream: bool = Field(default=False)  # Flag for streaming response
    
    @field_validator('message')
    def validate_message_length(cls, v):
        """Additional validation for message length and content"""
        if len(v) > 2000:
            raise ValueError("Message exceeds maximum allowed length")
        return v

class ConversationRequest(BaseModel):
    user_id: str = Field(..., min_length=3, max_length=50)
    title: Optional[str] = None

class TitleUpdateRequest(BaseModel):
    user_id: str = Field(..., min_length=3, max_length=50)
    conversation_id: int
    title: str = Field(..., min_length=1, max_length=100)