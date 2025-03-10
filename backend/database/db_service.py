import logging
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from contextlib import contextmanager

# Import sharded models
from backend.database.db_models import ShardRegistry, ShardConfig, User, Conversation, Message

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@contextmanager
def get_db_for_user(user_id: str):
    """Get the appropriate database session for a user"""
    shard_id, db = ShardRegistry.get_session_for_user(user_id)
    try:
        yield db, shard_id
    finally:
        db.close()

# Maintain original context manager for backward compatibility
@contextmanager
def get_db():
    """Original database session context manager (uses shard 0)"""
    db = ShardRegistry.get_session_maker(0)()
    try:
        yield db
    finally:
        db.close()

class ChatHistoryService:
    """Service for managing chat history storage and retrieval with sharding support"""
    
    @staticmethod
    def get_or_create_user(db: Session, user_id: str, shard_id: int = None) -> User:
        """Get existing user or create new one"""
        user = db.query(User).filter(User.user_id == user_id).first()
        
        if not user:
            # If shard_id wasn't explicitly provided, calculate it
            if shard_id is None:
                shard_id = ShardConfig.get_shard_for_user(user_id)
                
            user = User(user_id=user_id, shard_id=shard_id)
            db.add(user)
            db.commit()
            db.refresh(user)
            
        return user
    
    @staticmethod
    def get_or_create_conversation(db: Session, user_id: str, conversation_id: Optional[int] = None) -> Conversation:
        """Get existing conversation or create new one"""
        user = ChatHistoryService.get_or_create_user(db, user_id)
        
        if conversation_id:
            # Get specific conversation if ID provided
            conversation = db.query(Conversation).filter(
                Conversation.id == conversation_id,
                Conversation.user_id == user.id
            ).first()
            
            if not conversation:
                # Fallback to creating new if not found
                conversation = Conversation(user_id=user.id)
                db.add(conversation)
                db.commit()
                db.refresh(conversation)
        else:
            # Get most recent conversation or create new
            conversation = db.query(Conversation).filter(
                Conversation.user_id == user.id
            ).order_by(desc(Conversation.last_message_at)).first()
            
            if not conversation:
                conversation = Conversation(user_id=user.id)
                db.add(conversation)
                db.commit()
                db.refresh(conversation)
                
        return conversation
    
    @staticmethod
    def save_message_pair(
        db: Session, 
        user_id: str, 
        user_message: str, 
        assistant_message: str,
        conversation_id: Optional[int] = None,
        user_token_count: Optional[int] = None,
        assistant_token_count: Optional[int] = None,
        processing_time: Optional[float] = None,
        shard_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Save a user-assistant message pair to database"""
        try:
            # Get or create conversation
            conversation = ChatHistoryService.get_or_create_conversation(db, user_id, conversation_id)
            
            # Create user message
            user_msg = Message(
                conversation_id=conversation.id,
                is_user=1,
                content=user_message,
                token_count=user_token_count
            )
            db.add(user_msg)
            
            # Create assistant message
            assistant_msg = Message(
                conversation_id=conversation.id,
                is_user=0,
                content=assistant_message,
                token_count=assistant_token_count,
                processing_time=processing_time
            )
            db.add(assistant_msg)
            
            # Update conversation last message time
            conversation.last_message_at = func.now()
            
            db.commit()
            
            return {
                "success": True,
                "conversation_id": conversation.id,
                "user_message_id": user_msg.id,
                "assistant_message_id": assistant_msg.id
            }
            
        except Exception as e:
            db.rollback()
            logger.error(f"Error saving message pair: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    @staticmethod
    def get_conversation_history(
        db: Session, 
        user_id: str, 
        conversation_id: Optional[int] = None,
        limit: int = 50,
        shard_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Get conversation history for user"""
        try:
            user = ChatHistoryService.get_or_create_user(db, user_id, shard_id)
            
            if conversation_id:
                # Get messages from specific conversation
                messages = db.query(Message).join(
                    Conversation, Message.conversation_id == Conversation.id
                ).filter(
                    Conversation.id == conversation_id,
                    Conversation.user_id == user.id
                ).order_by(Message.created_at).all()
            else:
                # Get messages from most recent conversation
                conversation = db.query(Conversation).filter(
                    Conversation.user_id == user.id
                ).order_by(desc(Conversation.last_message_at)).first()
                
                if not conversation:
                    return []
                    
                messages = db.query(Message).filter(
                    Message.conversation_id == conversation.id
                ).order_by(Message.created_at).limit(limit).all()
                
            # Format messages
            result = []
            for i in range(0, len(messages) - 1, 2):
                if i + 1 < len(messages):
                    if messages[i].is_user == 1 and messages[i+1].is_user == 0:
                        result.append({
                            "user": messages[i].content,
                            "assistant": messages[i+1].content,
                            "timestamp": messages[i].created_at.isoformat(),
                        })
                        
            return result
            
        except Exception as e:
            logger.error(f"Error getting conversation history: {e}")
            return []
    
    # Wrapper methods that manage sharding internally
    
    @staticmethod
    def sharded_save_message_pair(
        user_id: str, 
        user_message: str, 
        assistant_message: str,
        conversation_id: Optional[int] = None,
        user_token_count: Optional[int] = None,
        assistant_token_count: Optional[int] = None,
        processing_time: Optional[float] = None
    ) -> Dict[str, Any]:
        """Save a message pair using the appropriate shard"""
        with get_db_for_user(user_id) as (db, shard_id):
            return ChatHistoryService.save_message_pair(
                db, user_id, user_message, assistant_message, 
                conversation_id, user_token_count, assistant_token_count, 
                processing_time, shard_id
            )
    
    @staticmethod
    def sharded_get_conversation_history(
        user_id: str, 
        conversation_id: Optional[int] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get conversation history using the appropriate shard"""
        with get_db_for_user(user_id) as (db, shard_id):
            return ChatHistoryService.get_conversation_history(
                db, user_id, conversation_id, limit, shard_id
            )
    
    @staticmethod
    def sharded_get_recent_context(
        user_id: str, 
        max_pairs: int = 3,
        conversation_id: Optional[int] = None
    ) -> List[str]:
        """Get recent context using the appropriate shard"""
        with get_db_for_user(user_id) as (db, shard_id):
            return ChatHistoryService.get_recent_context(
                db, user_id, max_pairs, conversation_id
            )
    
    @staticmethod
    def sharded_get_user_conversations(
        user_id: str, 
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Get user conversations using the appropriate shard"""
        with get_db_for_user(user_id) as (db, shard_id):
            return ChatHistoryService.get_user_conversations(
                db, user_id, limit
            )
    
    @staticmethod
    def sharded_create_new_conversation(
        user_id: str, 
        title: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a new conversation using the appropriate shard"""
        with get_db_for_user(user_id) as (db, shard_id):
            return ChatHistoryService.create_new_conversation(
                db, user_id, title
            )
    
    @staticmethod
    def sharded_update_conversation_title(
        user_id: str, 
        conversation_id: int, 
        title: str
    ) -> Dict[str, Any]:
        """Update conversation title using the appropriate shard"""
        with get_db_for_user(user_id) as (db, shard_id):
            return ChatHistoryService.update_conversation_title(
                db, user_id, conversation_id, title
            )
    
    @staticmethod
    def sharded_delete_conversation(
        user_id: str, 
        conversation_id: int
    ) -> Dict[str, Any]:
        """Delete a conversation using the appropriate shard"""
        with get_db_for_user(user_id) as (db, shard_id):
            return ChatHistoryService.delete_conversation(
                db, user_id, conversation_id
            )
            
    # Original methods kept for backward compatibility
    # These methods can be kept unchanged from your original implementation
    # They're included here for completeness
            
    @staticmethod
    def get_recent_context(
        db: Session, 
        user_id: str, 
        max_pairs: int = 3,
        conversation_id: Optional[int] = None
    ) -> List[str]:
        """Get recent context for model input"""
        try:
            # Get most recent conversation
            conversation = None
            if conversation_id:
                conversation = db.query(Conversation).filter(
                    Conversation.id == conversation_id
                ).first()
            
            if not conversation:
                user = ChatHistoryService.get_or_create_user(db, user_id)
                conversation = db.query(Conversation).filter(
                    Conversation.user_id == user.id
                ).order_by(desc(Conversation.last_message_at)).first()
            
            if not conversation:
                return []
                
            # Get most recent messages
            messages = db.query(Message).filter(
                Message.conversation_id == conversation.id
            ).order_by(desc(Message.created_at)).limit(max_pairs * 2).all()
            
            # Reorder from oldest to newest
            messages.reverse()
            
            # Extract as flat list of strings
            context = []
            for message in messages:
                context.append(message.content)
                
            return context
            
        except Exception as e:
            logger.error(f"Error getting recent context: {e}")
            return []
    
    @staticmethod
    def get_user_conversations(
        db: Session, 
        user_id: str, 
        limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Get list of conversations for a user"""
        try:
            user = ChatHistoryService.get_or_create_user(db, user_id)
            
            conversations = db.query(Conversation).filter(
                Conversation.user_id == user.id
            ).order_by(desc(Conversation.last_message_at)).limit(limit).all()
            
            result = []
            for conv in conversations:
                # Get first message as title preview
                first_message = db.query(Message).filter(
                    Message.conversation_id == conv.id,
                    Message.is_user == 1
                ).order_by(Message.created_at).first()
                
                # Get message count
                message_count = db.query(func.count(Message.id)).filter(
                    Message.conversation_id == conv.id
                ).scalar()
                
                result.append({
                    "id": conv.id,
                    "title": conv.title,
                    "preview": first_message.content[:50] + "..." if first_message else "Empty conversation",
                    "message_count": message_count,
                    "created_at": conv.created_at.isoformat(),
                    "last_message_at": conv.last_message_at.isoformat()
                })
                
            return result
            
        except Exception as e:
            logger.error(f"Error getting user conversations: {e}")
            return []
    
    @staticmethod
    def create_new_conversation(
        db: Session, 
        user_id: str, 
        title: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a new conversation for user"""
        try:
            user = ChatHistoryService.get_or_create_user(db, user_id)
            
            conversation = Conversation(
                user_id=user.id,
                title=title or "New Conversation"
            )
            db.add(conversation)
            db.commit()
            db.refresh(conversation)
            
            return {
                "success": True,
                "conversation_id": conversation.id,
                "title": conversation.title
            }
            
        except Exception as e:
            db.rollback()
            logger.error(f"Error creating conversation: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    @staticmethod
    def update_conversation_title(
        db: Session, 
        user_id: str, 
        conversation_id: int, 
        title: str
    ) -> Dict[str, Any]:
        """Update conversation title"""
        try:
            user = ChatHistoryService.get_or_create_user(db, user_id)
            
            conversation = db.query(Conversation).filter(
                Conversation.id == conversation_id,
                Conversation.user_id == user.id
            ).first()
            
            if not conversation:
                return {
                    "success": False,
                    "error": "Conversation not found"
                }
                
            conversation.title = title
            db.commit()
            
            return {
                "success": True,
                "conversation_id": conversation.id,
                "title": conversation.title
            }
            
        except Exception as e:
            db.rollback()
            logger.error(f"Error updating conversation title: {e}")
            return {
                "success": False,
                "error": str(e)
            }
    
    @staticmethod
    def delete_conversation(
        db: Session, 
        user_id: str, 
        conversation_id: int
    ) -> Dict[str, Any]:
        """Delete a conversation and all its messages"""
        try:
            user = ChatHistoryService.get_or_create_user(db, user_id)
            
            conversation = db.query(Conversation).filter(
                Conversation.id == conversation_id,
                Conversation.user_id == user.id
            ).first()
            
            if not conversation:
                return {
                    "success": False,
                    "error": "Conversation not found"
                }
                
            db.delete(conversation)
            db.commit()
            
            return {
                "success": True,
                "message": "Conversation deleted"
            }
            
        except Exception as e:
            db.rollback()
            logger.error(f"Error deleting conversation: {e}")
            return {
                "success": False,
                "error": str(e)
            }