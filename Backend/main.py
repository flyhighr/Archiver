from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Dict, Any, Union
from datetime import datetime
import logging
import asyncio
from contextlib import asynccontextmanager
import signal
import sys
from logging.handlers import RotatingFileHandler
import aiohttp
import time
from functools import wraps
import os
import uuid
import hashlib
import random
import string
from bson import ObjectId

# Enhanced logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler(
            'app.log',
            maxBytes=5*1024*1024,  # 5MB
            backupCount=10
        ),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Enhanced Base Models with improved type handling
class BaseModelWithConfig(BaseModel):
    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        populate_by_name=True,
        from_attributes=True,
        json_encoders={ObjectId: str}
    )

# Enhanced User Profile Model
class UserProfile(BaseModelWithConfig):
    id: str
    username: str
    global_name: Optional[str] = None
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    banner_url: Optional[str] = None
    bio: Optional[str] = None
    member_since: Optional[str] = None
    accent_color: Optional[int] = None
    badges: List[str] = Field(default_factory=list)
    status: Optional[str] = None
    custom_status: Optional[str] = None

# Enhanced Friendship Status Model
class FriendshipStatus(BaseModelWithConfig):
    is_friend: bool = False
    friend_since: Optional[str] = None
    relationship_type: Optional[str] = None  # friend, blocked, pending, etc.

# Enhanced Author Model (extends UserProfile)
class Author(UserProfile):
    friendship: Optional[FriendshipStatus] = None

# Enhanced Reply Reference Model
class ReplyReference(BaseModelWithConfig):
    message_id: str
    author: Author
    content: str
    timestamp: Optional[str] = None

# Enhanced Attachment Model with better media handling
class Attachment(BaseModelWithConfig):
    url: str
    filename: str
    content_type: str
    size: int
    width: Optional[int] = None
    height: Optional[int] = None
    proxy_url: Optional[str] = None
    is_spoiler: bool = False
    description: Optional[str] = None
    duration: Optional[float] = None  # For audio/video

# Enhanced Reaction User Model
class ReactionUser(UserProfile):
    pass

# Enhanced Reaction Model
class Reaction(BaseModelWithConfig):
    emoji: str
    emoji_id: Optional[str] = None
    emoji_name: Optional[str] = None
    emoji_url: Optional[str] = None
    count: int
    users: List[ReactionUser] = Field(default_factory=list)
    me: bool = False  # Whether the authenticated user reacted

# Enhanced Embed Model
class Embed(BaseModelWithConfig):
    title: Optional[str] = None
    type: Optional[str] = None
    description: Optional[str] = None
    url: Optional[str] = None
    timestamp: Optional[str] = None
    color: Optional[int] = None
    footer: Optional[Dict[str, Any]] = None
    image: Optional[Dict[str, Any]] = None
    thumbnail: Optional[Dict[str, Any]] = None
    video: Optional[Dict[str, Any]] = None
    provider: Optional[Dict[str, Any]] = None
    author: Optional[Dict[str, Any]] = None
    fields: List[Dict[str, Any]] = Field(default_factory=list)

# Enhanced Message Model
class Message(BaseModelWithConfig):
    content: str
    author: Author
    timestamp: str
    message_id: str
    channel_id: str
    attachments: List[Attachment] = Field(default_factory=list)
    reply_to: Optional[ReplyReference] = None
    reactions: List[Reaction] = Field(default_factory=list)
    embeds: List[Embed] = Field(default_factory=list)
    edited_timestamp: Optional[str] = None
    pinned: bool = False
    type: Optional[int] = None
    is_deleted: bool = False
    mentions: List[UserProfile] = Field(default_factory=list)
    mention_roles: List[str] = Field(default_factory=list)
    mention_everyone: bool = False
    reference_id: Optional[str] = None  # For cross-posting/referencing

# Enhanced Channel Model
class Channel(BaseModelWithConfig):
    channel_id: str
    name: Optional[str] = None
    type: int
    topic: Optional[str] = None
    position: Optional[int] = None
    nsfw: bool = False
    last_message_id: Optional[str] = None
    guild_id: Optional[str] = None
    parent_id: Optional[str] = None
    rate_limit_per_user: Optional[int] = None
    icon: Optional[str] = None
    recipients: List[UserProfile] = Field(default_factory=list)

# Enhanced Conversation Model
class Conversation(BaseModelWithConfig):
    conversation_id: str
    messages: List[Message]
    channel_id: str
    channel_info: Optional[Channel] = None
    created_at: str
    share_url: Optional[str] = None
    pinned_messages: List[Message] = Field(default_factory=list)
    participants: List[UserProfile] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    last_updated: Optional[str] = None
    message_count: Optional[int] = None
    deleted_message_count: Optional[int] = None
    is_group_dm: bool = False
    dm_name: Optional[str] = None

# Enhanced Database class with improved connection handling
class Database:
    client: Optional[AsyncIOMotorClient] = None
    db = None
    MONGO_URI = os.getenv("MONGO_URI")
    MAX_RETRIES = 5
    RETRY_DELAY = 5
    COLLECTIONS = ["conversations", "users", "channels", "metrics"]

    @classmethod
    async def connect_db(cls):
        retries = 0
        while retries < cls.MAX_RETRIES:
            try:
                logger.info(f"Connecting to MongoDB (attempt {retries + 1}/{cls.MAX_RETRIES})...")
                cls.client = AsyncIOMotorClient(
                    cls.MONGO_URI,
                    serverSelectionTimeoutMS=10000,
                    connectTimeoutMS=20000,
                    retryWrites=True,
                    w="majority"
                )
                cls.db = cls.client.discord_archives
                
                # Verify connection
                await cls.client.admin.command('ping')
                
                # Initialize indexes for better performance
                await cls._create_indexes()
                
                logger.info("Successfully connected to MongoDB")
                return
            except Exception as e:
                retries += 1
                if retries == cls.MAX_RETRIES:
                    logger.error(f"Failed to connect to MongoDB after {cls.MAX_RETRIES} attempts: {e}")
                    raise
                logger.warning(f"Failed to connect to MongoDB (attempt {retries}): {e}")
                await asyncio.sleep(cls.RETRY_DELAY)

    @classmethod
    async def _create_indexes(cls):
        """Create indexes for better query performance"""
        try:
            # Conversation indexes
            await cls.db.conversations.create_index("conversation_id", unique=True)
            await cls.db.conversations.create_index("channel_id")
            await cls.db.conversations.create_index("created_at")
            
            # Message indexes (if stored separately)
            if "messages" in cls.COLLECTIONS:
                await cls.db.messages.create_index("message_id")
                await cls.db.messages.create_index("conversation_id")
                
            # User indexes
            await cls.db.users.create_index("id", unique=True)
            
            logger.info("Database indexes created successfully")
        except Exception as e:
            logger.error(f"Error creating database indexes: {e}")

    @classmethod
    async def close_db(cls):
        if cls.client:
            logger.info("Closing MongoDB connection...")
            cls.client.close()
            logger.info("MongoDB connection closed")

# Enhanced Self-Ping service for improved reliability
class SelfPing:
    def __init__(self, url: str = None, interval: int = 840):
        self.url = url or os.getenv("RENDER_URL", "https://api-v9ww.onrender.com") + '/health'
        self.interval = interval
        self.session: Optional[aiohttp.ClientSession] = None
        self.is_running = False
        self.last_ping_time = 0
        self._task: Optional[asyncio.Task] = None
        self._retries = 0
        self.MAX_RETRIES = 3

    async def start(self):
        if self._task is not None:
            return

        self.is_running = True
        self.session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._run())
        logger.info("Self-ping service started")

    async def _run(self):
        while self.is_running:
            try:
                current_time = time.time()
                if current_time - self.last_ping_time >= self.interval:
                    async with self.session.get(self.url, timeout=10) as response:
                        if response.status == 200:
                            logger.info("Self-ping successful")
                            self.last_ping_time = current_time
                            self._retries = 0
                        else:
                            logger.warning(f"Self-ping failed: {response.status}")
                            self._retries += 1
            except Exception as e:
                logger.error(f"Self-ping error: {e}")
                self._retries += 1
                
            if self._retries >= self.MAX_RETRIES:
                logger.warning(f"Self-ping failed {self._retries} times in a row")
                self._retries = 0
                
            await asyncio.sleep(60)

    async def stop(self):
        self.is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        
        if self.session:
            await self.session.close()
            
        logger.info("Self-ping service stopped")

# Enhanced shutdown handler
async def shutdown():
    logger.info("Initiating graceful shutdown...")
    
    # Add any cleanup tasks here
    tasks = []
    for task in asyncio.all_tasks():
        if task is not asyncio.current_task():
            task.cancel()
            tasks.append(task)
    
    await asyncio.gather(*tasks, return_exceptions=True)
    await Database.close_db()
    logger.info("Shutdown complete")

# Enhanced application lifecycle management
@asynccontextmanager
async def lifespan(app: FastAPI):
    ping_service = SelfPing()
    try:
        # Initialize services
        await Database.connect_db()
        await ping_service.start()
        
        # Set up signal handlers
        def signal_handler(sig, frame):
            logger.info(f"Received signal {sig}")
            asyncio.create_task(shutdown())

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        logger.info("Application startup complete")
        yield
    finally:
        # Cleanup
        await ping_service.stop()
        await Database.close_db()
        logger.info("Application shutdown complete")

# Initialize FastAPI application
app = FastAPI(
    title="Discord Archiver API",
    description="Enhanced API for managing Discord conversation archives with comprehensive user data",
    version="2.0.0",
    lifespan=lifespan
)

# Performance monitoring decorator
def monitor_performance():
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            start_time = time.time()
            try:
                result = await func(*args, **kwargs)
                return result
            finally:
                execution_time = time.time() - start_time
                logger.info(f"{func.__name__} executed in {execution_time:.2f} seconds")
        return wrapper
    return decorator

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Enhanced error handling middleware
@app.middleware("http")
async def error_handling_middleware(request, call_next):
    try:
        start_time = time.time()
        response = await call_next(request)
        process_time = time.time() - start_time
        response.headers["X-Process-Time"] = str(process_time)
        return response
    except Exception as e:
        logger.error(f"Unhandled error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error :(")

# Enhanced URL generator for secure, unique URLs
def generate_secure_url_id(conversation_id: str, salt: str = None) -> str:
    """Generate a secure, non-guessable URL ID based on the conversation ID"""
    if not salt:
        salt = ''.join(random.choices(string.ascii_letters + string.digits, k=8))
    
    # Create a hash of the conversation ID with salt
    hash_base = f"{conversation_id}:{salt}:{time.time()}"
    hash_obj = hashlib.sha256(hash_base.encode())
    hash_digest = hash_obj.hexdigest()
    
    # Use a portion of the hash for the URL (12 chars is sufficient)
    url_id = hash_digest[:12]
    
    return url_id

# Utility function to extract and process user profiles
async def process_user_profiles(conversation_data: dict) -> List[UserProfile]:
    """Extract and process user profiles from conversation data"""
    users = {}
    
    # Process message authors
    for message in conversation_data.get("messages", []):
        if "author" in message and "id" in message["author"]:
            author_id = message["author"]["id"]
            if author_id not in users:
                users[author_id] = message["author"]
    
    # Process pinned message authors
    for message in conversation_data.get("pinned_messages", []):
        if "author" in message and "id" in message["author"]:
            author_id = message["author"]["id"]
            if author_id not in users:
                users[author_id] = message["author"]
    
    # Process channel recipients if available
    if "channel_info" in conversation_data and "recipients" in conversation_data["channel_info"]:
        for recipient in conversation_data["channel_info"]["recipients"]:
            if "id" in recipient:
                recipient_id = recipient["id"]
                if recipient_id not in users:
                    users[recipient_id] = recipient
    
    # Convert to list of UserProfile objects
    return [UserProfile(**user_data) for user_data in users.values()]

# Enhanced conversation creation endpoint
@app.post("/conversations/")
@monitor_performance()
async def create_conversation(conversation: Conversation):
    try:
        # Convert to dict for MongoDB storage
        conversation_dict = conversation.model_dump()
        
        # Add metadata and counts
        conversation_dict["last_updated"] = datetime.utcnow().isoformat()
        conversation_dict["message_count"] = len(conversation_dict.get("messages", []))
        conversation_dict["deleted_message_count"] = sum(
            1 for msg in conversation_dict.get("messages", []) if msg.get("is_deleted", False)
        )
        
        # Extract participants from messages and channel info
        participants = await process_user_profiles(conversation_dict)
        conversation_dict["participants"] = [user.model_dump() for user in participants]
        
        # Generate a secure, unique URL ID
        url_id = generate_secure_url_id(conversation.conversation_id)
        base_url = os.getenv("BASE_URL", "https://archi.versz.fun")
        share_url = f"{base_url}?id={conversation.conversation_id}&v={url_id}"
        conversation_dict["share_url"] = share_url
        
        # Determine if this is a group DM
        is_group_dm = False
        dm_name = None
        
        if "channel_info" in conversation_dict and conversation_dict["channel_info"]:
            channel_type = conversation_dict["channel_info"].get("type")
            # Type 3 is group DM in Discord
            if channel_type == 3:
                is_group_dm = True
                dm_name = conversation_dict["channel_info"].get("name")
        
        conversation_dict["is_group_dm"] = is_group_dm
        conversation_dict["dm_name"] = dm_name
        
        # Insert into database
        result = await Database.db.conversations.insert_one(conversation_dict)
        
        # Store users separately for future reference
        for user in participants:
            user_dict = user.model_dump()
            # Upsert to avoid duplicates
            await Database.db.users.update_one(
                {"id": user_dict["id"]},
                {"$set": user_dict},
                upsert=True
            )
        
        logger.info(f"Created conversation: {conversation.conversation_id} with {len(participants)} participants")
        
        return {
            "conversation_id": conversation.conversation_id,
            "inserted_id": str(result.inserted_id),
            "share_url": share_url,
            "message_count": conversation_dict["message_count"],
            "participants_count": len(participants)
        }
    except Exception as e:
        logger.error(f"Error creating conversation: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create conversation :(")

# Enhanced conversation retrieval endpoint
@app.get("/conversations/{conversation_id}")
@monitor_performance()
async def get_conversation(conversation_id: str, include_deleted: bool = False):
    try:
        conversation = await Database.db.conversations.find_one({"conversation_id": conversation_id})
        if not conversation:
            logger.warning(f"Conversation not found: {conversation_id}")
            raise HTTPException(status_code=404, detail="Conversation not found :(")
        
        # Convert ObjectId to string
        conversation["_id"] = str(conversation["_id"])
        
        # Filter deleted messages if requested
        if not include_deleted:
            conversation["messages"] = [
                msg for msg in conversation["messages"] 
                if not msg.get("is_deleted", False)
            ]
        
        # Sort messages by timestamp
        conversation["messages"].sort(key=lambda x: x["timestamp"])
        
        # Update message count based on filtering
        conversation["message_count"] = len(conversation["messages"])
        
        return conversation
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving conversation {conversation_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve conversation :(")

# Enhanced share URL generation endpoint
@app.get("/conversations/{conversation_id}/share-url")
@monitor_performance()
async def get_share_url(conversation_id: str, regenerate: bool = False):
    try:
        conversation = await Database.db.conversations.find_one({"conversation_id": conversation_id})
        if not conversation:
            logger.warning(f"Conversation not found: {conversation_id}")
            raise HTTPException(status_code=404, detail="Conversation not found")
        
        # If URL exists and we're not regenerating, return it
        if "share_url" in conversation and conversation["share_url"] and not regenerate:
            return {"share_url": conversation["share_url"]}
        
        # Generate a new secure URL
        url_id = generate_secure_url_id(conversation_id)
        base_url = os.getenv("BASE_URL", "https://archi.versz.fun")
        share_url = f"{base_url}?id={conversation_id}&v={url_id}"
        
        # Update in database
        await Database.db.conversations.update_one(
            {"conversation_id": conversation_id},
            {"$set": {"share_url": share_url}}
        )
        
        logger.info(f"Generated share URL for conversation: {conversation_id}")
        return {"share_url": share_url}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating share URL for conversation {conversation_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to generate share URL")

# Enhanced conversation listing endpoint
@app.get("/conversations")
@monitor_performance()
async def list_conversations(
    channel_id: Optional[str] = None,
    user_id: Optional[str] = None,
    is_group_dm: Optional[bool] = None,
    limit: int = Query(10, ge=1, le=100),
    skip: int = Query(0, ge=0),
    sort_by: str = "created_at",
    sort_order: int = -1  # -1 for descending, 1 for ascending
):
    try:
        # Build query
        query = {}
        if channel_id:
            query["channel_id"] = channel_id
        
        if user_id:
            query["participants.id"] = user_id
            
        if is_group_dm is not None:
            query["is_group_dm"] = is_group_dm
        
        # Validate sort field
        valid_sort_fields = ["created_at", "message_count", "last_updated"]
        if sort_by not in valid_sort_fields:
            sort_by = "created_at"
            
        # Validate sort order
        if sort_order not in [-1, 1]:
            sort_order = -1

        # Count total matching documents
        total_count = await Database.db.conversations.count_documents(query)
        
        # Projection to limit returned fields for performance
        projection = {
            "_id": 1,
            "conversation_id": 1,
            "channel_id": 1,
            "created_at": 1,
            "last_updated": 1,
            "share_url": 1,
            "message_count": 1,
            "deleted_message_count": 1,
            "is_group_dm": 1,
            "dm_name": 1,
            "participants": 1,
            "channel_info.name": 1,
            "channel_info.type": 1,
            "channel_info.recipients": 1
        }
        
        # Execute query with pagination
        conversations = await Database.db.conversations.find(
            query, 
            projection
        ).sort(sort_by, sort_order).skip(skip).limit(limit).to_list(length=limit)
        
        # Convert ObjectId to string
        for conv in conversations:
            conv["_id"] = str(conv["_id"])
        
        return {
            "conversations": conversations,
            "total": total_count,
            "page": skip // limit + 1,
            "pages": (total_count + limit - 1) // limit,
            "limit": limit,
            "skip": skip
        }
    except Exception as e:
        logger.error(f"Error listing conversations: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to list conversations :( ")

# New endpoint to fetch user profiles
@app.get("/users/{user_id}")
@monitor_performance()
async def get_user_profile(user_id: str):
    try:
        user = await Database.db.users.find_one({"id": user_id})
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        user["_id"] = str(user["_id"])
        return user
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve user profile")

# New endpoint to fetch pinned messages
@app.get("/conversations/{conversation_id}/pinned")
@monitor_performance()
async def get_pinned_messages(conversation_id: str):
    try:
        conversation = await Database.db.conversations.find_one(
            {"conversation_id": conversation_id},
            {"pinned_messages": 1}
        )
        
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        
        pinned_messages = conversation.get("pinned_messages", [])
        return {"pinned_messages": pinned_messages, "count": len(pinned_messages)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving pinned messages for {conversation_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve pinned messages")

# New endpoint to add pinned messages
@app.post("/conversations/{conversation_id}/pinned")
@monitor_performance()
async def add_pinned_message(conversation_id: str, message: Message):
    try:
        # Check if conversation exists
        conversation = await Database.db.conversations.find_one({"conversation_id": conversation_id})
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        
        # Add pinned flag to message
        message_dict = message.model_dump()
        message_dict["pinned"] = True
        
        # Update the conversation
        result = await Database.db.conversations.update_one(
            {"conversation_id": conversation_id},
            {"$push": {"pinned_messages": message_dict}}
        )
        
        if result.modified_count == 0:
            raise HTTPException(status_code=500, detail="Failed to add pinned message")
        
        return {"status": "success", "message": "Pinned message added"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding pinned message to {conversation_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to add pinned message")

# New endpoint for user statistics
@app.get("/users/{user_id}/statistics")
@monitor_performance()
async def get_user_statistics(user_id: str):
    try:
        # Find conversations where user is a participant
        conversation_count = await Database.db.conversations.count_documents({
            "participants.id": user_id
        })
        
        # Aggregate message count by user
        pipeline = [
            {"$match": {"participants.id": user_id}},
            {"$unwind": "$messages"},
            {"$match": {"messages.author.id": user_id}},
            {"$count": "message_count"}
        ]
        
        result = await Database.db.conversations.aggregate(pipeline).to_list(length=1)
        message_count = result[0]["message_count"] if result else 0
        
        return {
            "user_id": user_id,
            "conversation_count": conversation_count,
            "message_count": message_count,
            "last_updated": datetime.utcnow().isoformat()
        }
    except Exception as e:
        logger.error(f"Error getting statistics for user {user_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve user statistics")

# Enhanced health check endpoint
@app.get("/health")
@monitor_performance()
async def health_check():
    try:
        # Check database connection
        db_start = time.time()
        await Database.client.admin.command('ping')
        db_time = time.time() - db_start
        
        # Check system resources
        memory_info = {}
        try:
            import psutil
            memory = psutil.virtual_memory()
            memory_info = {
                "total": memory.total,
                "available": memory.available,
                "percent": memory.percent
            }
        except ImportError:
            memory_info = {"error": "psutil not available"}
        
        return {
            "status": "healthy",
            "timestamp": datetime.utcnow().isoformat(),
            "version": "2.0.0",
            "database": {
                "status": "connected",
                "response_time_ms": round(db_time * 1000, 2)
            },
            "system": {
                "memory": memory_info
            }
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail="Service unhealthy")

# Configure server port
PORT = int(os.getenv("PORT", 10000))

# Run the application
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=PORT,
        workers=1,
        log_level="info"
    )
