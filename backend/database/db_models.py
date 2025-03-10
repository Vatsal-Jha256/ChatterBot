from sqlalchemy import Column, Integer, String, Text, Float, DateTime, ForeignKey, Index, create_engine
from sqlalchemy.orm import relationship, sessionmaker, declarative_base
from sqlalchemy.sql import func
import os
from datetime import datetime
from sqlalchemy.sql import text
import hashlib

# Base configuration
Base = declarative_base()

class ShardConfig:
    """Configuration for database sharding"""
    # Number of shards to use
    SHARD_COUNT = 4
    
    # Default connection string template
    SHARD_CONNECTION_TEMPLATE = "sqlite:///./chat_history_shard_{}.db"
    
    @staticmethod
    def get_shard_connection_string(shard_id):
        """Get connection string for a specific shard"""
        template = os.getenv("SHARD_CONNECTION_TEMPLATE", ShardConfig.SHARD_CONNECTION_TEMPLATE)
        return template.format(shard_id)
    
    @staticmethod
    def get_shard_for_user(user_id):
        """Determine which shard to use for a given user ID"""
        # Create a consistent hash from the user_id
        hash_obj = hashlib.md5(user_id.encode())
        hash_int = int(hash_obj.hexdigest(), 16)
        
        # Deterministically assign to a shard
        return hash_int % ShardConfig.SHARD_COUNT

# Keep the original models unchanged
class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String(50), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_active = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    shard_id = Column(Integer, nullable=False)  # Store which shard this user belongs to
    
    # Relationships
    conversations = relationship("Conversation", back_populates="user", cascade="all, delete-orphan")
    
    # Create index on user_id for faster lookups
    __table_args__ = (Index("idx_user_user_id", user_id),)

class Conversation(Base):
    __tablename__ = "conversations"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(255), default="New Conversation")
    created_at = Column(DateTime, default=datetime.utcnow)
    last_message_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationships
    user = relationship("User", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")
    
    # Create composite index on user_id and last_message_at for recent conversation queries
    __table_args__ = (Index("idx_conversation_user_last", user_id, last_message_at.desc()),)

class Message(Base):
    __tablename__ = "messages"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    is_user = Column(Integer, nullable=False, default=1)  # 1 for user, 0 for assistant
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Additional fields for analytics
    token_count = Column(Integer, nullable=True)
    processing_time = Column(Float, nullable=True)
    
    # Relationships
    conversation = relationship("Conversation", back_populates="messages")
    
    # Create index on conversation_id for faster message retrieval
    __table_args__ = (Index("idx_message_conversation", conversation_id),)

# Create a registry of database engines and session makers
class ShardRegistry:
    """Registry for shard database connections"""
    _engines = {}
    _session_makers = {}
    
    @classmethod
    def get_engine(cls, shard_id):
        """Get or create engine for a specific shard"""
        if shard_id not in cls._engines:
            connection_string = ShardConfig.get_shard_connection_string(shard_id)
            cls._engines[shard_id] = create_engine(
                connection_string,
                pool_size=20,
                max_overflow=30,
                pool_recycle=3600,
                pool_pre_ping=True,
                echo=False
            )
        return cls._engines[shard_id]
    
    @classmethod
    def get_session_maker(cls, shard_id):
        """Get or create session maker for a specific shard"""
        if shard_id not in cls._session_makers:
            engine = cls.get_engine(shard_id)
            cls._session_makers[shard_id] = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        return cls._session_makers[shard_id]
    
    @classmethod
    def initialize_all_shards(cls):
        """Initialize all shard databases"""
        for shard_id in range(ShardConfig.SHARD_COUNT):
            engine = cls.get_engine(shard_id)
            Base.metadata.create_all(bind=engine)
            print(f"Initialized shard {shard_id}")
    
    @classmethod
    def get_session_for_user(cls, user_id):
        """Get appropriate session for a user ID"""
        shard_id = ShardConfig.get_shard_for_user(user_id)
        session_maker = cls.get_session_maker(shard_id)
        return shard_id, session_maker()

# Compatibility function to maintain the original interface
def get_session_maker():
    """
    Compatibility function that returns a session maker for the default shard.
    This is used by code that expects the original function to work.
    In practice, this should rarely be used as the ShardRegistry methods
    are preferred for proper sharding.
    """
    return ShardRegistry.get_session_maker(0)

def init_db():
    """Initialize all shard databases"""
    ShardRegistry.initialize_all_shards()
    print("All shard databases initialized")

def print_schema():
    """Print schema for all shards"""
    for shard_id in range(ShardConfig.SHARD_COUNT):
        engine = ShardRegistry.get_engine(shard_id)
        from sqlalchemy import inspect
        inspector = inspect(engine)
        
        print(f"\n--- Schema for shard {shard_id} ---")
        for table_name in inspector.get_table_names():
            print(f"\nTable: {table_name}")
            for column in inspector.get_columns(table_name):
                print(f"{column['name']} - {column['type']}")

if __name__ == "__main__":
    init_db()
    print_schema()