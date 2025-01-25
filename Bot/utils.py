import discord
import aiohttp
from datetime import datetime, timezone
import uuid
import aiosqlite
from cryptography.fernet import Fernet
from typing import Optional, Union, Dict, List, Set
import logging
import asyncio
from contextlib import asynccontextmanager

logging.basicConfig(
    filename='bot.log',
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class DatabaseError(Exception):
    """Custom exception for database-related errors"""
    pass

class DMTracker:
    def __init__(self, user_id: str, channel_ids: List[str]):
        self.user_id = user_id
        self.channel_ids = set(channel_ids)
        
        self.messages: Dict[str, Dict[str, dict]] = {channel_id: {} for channel_id in channel_ids}
        self.deleted_messages: Dict[str, Dict[str, dict]] = {channel_id: {} for channel_id in channel_ids}
        self.start_time = datetime.now(timezone.utc)
        
        self.conversation_ids: Dict[str, str] = {
            channel_id: str(uuid.uuid4()) for channel_id in channel_ids
        }
        self.last_check_time = datetime.now(timezone.utc)
        self.message_cache: Dict[str, Set[str]] = {channel_id: set() for channel_id in channel_ids}
        self.message_contents: Dict[str, Dict[str, str]] = {
            channel_id: {} for channel_id in channel_ids
        }

    async def add_message(self, message_data: dict):
        try:
            message_id = message_data['message_id']
            channel_id = message_data['channel_id']
            
            if channel_id in self.channel_ids:
                self.messages[channel_id][message_id] = message_data
                self.message_contents[channel_id][message_id] = message_data.get('content', '')
                self.message_cache[channel_id].add(message_id)
                
        except KeyError as e:
            logger.error(f"Missing required key in message_data: {e}")
            raise ValueError(f"Invalid message data format: missing {e}")
        except Exception as e:
            logger.error(f"Error adding message: {e}")
            raise

    async def mark_as_deleted(self, message_id: str, channel_id: str):
        try:
            if channel_id not in self.channel_ids:
                raise ValueError(f"Invalid channel ID: {channel_id}")
                
            if message_id in self.messages[channel_id]:
                message_data = self.messages[channel_id][message_id].copy()
                message_data['is_deleted'] = True
                message_data['content'] = f"[deleted-msg] {message_data['content']}"
                self.deleted_messages[channel_id][message_id] = message_data
                self.message_cache[channel_id].remove(message_id)
                del self.messages[channel_id][message_id]
                del self.message_contents[channel_id][message_id]
                logger.info(f"Marked message {message_id} as deleted in channel {channel_id}")
        except Exception as e:
            logger.error(f"Error marking message as deleted: {e}")
            raise

class TokenManager:
    def __init__(self, db_path: str = 'user_tokens.db'):
        self.db_path = db_path
        self.ENCRYPTION_KEY = Fernet.generate_key()
        self.fernet = Fernet(self.ENCRYPTION_KEY)
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def get_connection(self):
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                await conn.execute('PRAGMA journal_mode=WAL')
                await conn.execute('PRAGMA synchronous=NORMAL')
                yield conn
        except aiosqlite.Error as e:
            logger.error(f"Database connection error: {e}")
            raise DatabaseError(f"Failed to connect to database: {e}")

    async def setup_database(self):
        try:
            async with self.get_connection() as conn:
                await conn.execute('''
                CREATE TABLE IF NOT EXISTS user_tokens (
                    user_id TEXT PRIMARY KEY,
                    encrypted_token TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used TIMESTAMP,
                    attempts INTEGER DEFAULT 0
                )
                ''')
                await conn.commit()
        except DatabaseError as e:
            logger.error(f"Failed to setup database: {e}")
            raise

    async def store_token(self, user_id: str, token: str):
        try:
            encrypted_token = self.fernet.encrypt(token.encode())
            async with self._lock:
                async with self.get_connection() as conn:
                    await conn.execute('''
                    INSERT OR REPLACE INTO user_tokens 
                    (user_id, encrypted_token, last_used, attempts) 
                    VALUES (?, ?, CURRENT_TIMESTAMP, 0)
                    ''', (user_id, encrypted_token))
                    await conn.commit()
        except Exception as e:
            logger.error(f"Error storing token: {e}")
            raise DatabaseError(f"Failed to store token: {e}")

    async def get_token(self, user_id: str) -> Optional[str]:
        try:
            async with self._lock:
                async with self.get_connection() as conn:
                    async with conn.execute(
                        'SELECT encrypted_token, attempts FROM user_tokens WHERE user_id = ?', 
                        (user_id,)
                    ) as cursor:
                        result = await cursor.fetchone()
                        
                    if not result:
                        return None
                        
                    encrypted_token, attempts = result
                    
                    if attempts >= 3:
                        logger.warning(f"Too many failed attempts for user {user_id}")
                        return None
                        
                    try:
                        decrypted_token = self.fernet.decrypt(encrypted_token).decode()
                        await conn.execute('''
                        UPDATE user_tokens 
                        SET last_used = CURRENT_TIMESTAMP, attempts = 0 
                        WHERE user_id = ?
                        ''', (user_id,))
                        await conn.commit()
                        return decrypted_token
                    except Exception:
                        await conn.execute('''
                        UPDATE user_tokens 
                        SET attempts = attempts + 1 
                        WHERE user_id = ?
                        ''', (user_id,))
                        await conn.commit()
                        return None
                        
        except Exception as e:
            logger.error(f"Error retrieving token: {e}")
            raise DatabaseError(f"Failed to retrieve token: {e}")

    async def delete_token(self, user_id: str):
        try:
            async with self._lock:
                async with self.get_connection() as conn:
                    await conn.execute('DELETE FROM user_tokens WHERE user_id = ?', (user_id,))
                    await conn.commit()
        except Exception as e:
            logger.error(f"Error deleting token: {e}")
            raise DatabaseError(f"Failed to delete token: {e}")

    async def cleanup_old_tokens(self, days: int = 30):
        try:
            async with self._lock:
                async with self.get_connection() as conn:
                    await conn.execute('''
                    DELETE FROM user_tokens 
                    WHERE julianday('now') - julianday(last_used) > ?
                    ''', (days,))
                    await conn.commit()
        except Exception as e:
            logger.error(f"Error cleaning up old tokens: {e}")
            raise DatabaseError(f"Failed to cleanup old tokens: {e}")

class MessageRecorder:
    def __init__(self, bot, channel_id: str, token: Optional[str] = None, session: Optional[aiohttp.ClientSession] = None):
        self.channel_id = channel_id
        self.token = token
        self.session = session
        self.messages = []
        self.conversation_id = str(uuid.uuid4())
        self.is_recording = True
        self.start_time = datetime.now(timezone.utc)
        self.headers = {"authorization": token} if token else {}
        self.last_message_id = None
        self.use_token_based = bool(token)
        self.bot = bot

    async def record_message(self, message: Union[discord.Message, dict]) -> bool:
        try:
            if self.use_token_based:
                await self.fetch_new_messages()
                return True
            else:
                processed_message = await self.process_message(message)
                self.messages.append(processed_message)
                return True
        except Exception as e:
            logger.error(f"Error recording message: {e}")
            return False

    async def fetch_new_messages(self):
        if not self.use_token_based or not self.session:
            return

        try:
            params = {"limit": 100}
            if self.last_message_id:
                params["after"] = self.last_message_id

            async with self.session.get(
                f"https://discord.com/api/v9/channels/{self.channel_id}/messages",
                headers=self.headers,
                params=params
            ) as response:
                if response.status == 200:
                    new_messages = await response.json()
                    
                    new_messages.sort(key=lambda x: x["id"])
                    
                    for msg in new_messages:
                        msg_time = datetime.fromisoformat(msg["timestamp"].rstrip('Z')).replace(tzinfo=timezone.utc)
                        if msg_time >= self.start_time:
                            processed_message = await self.process_message(msg)
                            
                            if not any(existing["message_id"] == processed_message["message_id"] 
                                     for existing in self.messages):
                                self.messages.append(processed_message)
                    
                    if new_messages:
                        self.last_message_id = new_messages[-1]["id"]
                        
        except Exception as e:
            logger.error(f"Error fetching new messages: {e}")

    async def stop_recording(self) -> list:
        self.is_recording = False
        if self.use_token_based:
            await self.fetch_new_messages()
        return self.messages

    @staticmethod
    def format_author(author) -> dict:
        return {
            "id": str(author.id) if hasattr(author, 'id') else str(author.get('id')),
            "username": author.name if hasattr(author, 'name') else author.get('username', 'Unknown'),
            "global_name": getattr(author, "global_name", None) if hasattr(author, 'global_name') else author.get('global_name'),
            "avatar_url": str(author.avatar.url) if (hasattr(author, 'avatar') and author.avatar) else 
                         (f"https://cdn.discordapp.com/avatars/{author.get('id')}/{author.get('avatar')}.png" 
                          if author.get('avatar') else None)
        }

    async def process_message(self, message: Union[discord.Message, dict]) -> dict:
        try:
            if isinstance(message, discord.Message):
                author = self.format_author(message.author)
                content = message.content
                timestamp = message.created_at.replace(tzinfo=timezone.utc).isoformat()
                message_id = str(message.id)
                
                attachments = [{
                    "url": att.url,
                    "filename": att.filename,
                    "content_type": getattr(att, "content_type", "unknown"),
                    "size": getattr(att, "size", 0)
                } for att in message.attachments]
                
                reply_data = None
                if message.reference and message.reference.message_id:
                    try:
                        referenced_msg = await message.channel.fetch_message(message.reference.message_id)
                        reply_data = {
                            "message_id": str(referenced_msg.id),
                            "author": self.format_author(referenced_msg.author),
                            "content": referenced_msg.content
                        }
                    except Exception:
                        pass
            else:
                author = self.format_author(message["author"])
                content = message.get("content", "")
                timestamp = message["timestamp"]
                message_id = str(message["id"])
                
                attachments = [{
                    "url": att["url"],
                    "filename": att["filename"],
                    "content_type": att.get("content_type", "unknown"),
                    "size": att.get("size", 0)
                } for att in message.get("attachments", [])]
                
                reply_data = None
                if message.get("referenced_message"):
                    ref_msg = message["referenced_message"]
                    reply_data = {
                        "message_id": str(ref_msg["id"]),
                        "author": self.format_author(ref_msg["author"]),
                        "content": ref_msg.get("content", "")
                    }

            reactions = await self.fetch_reactions(message_id, message)
            
            return {
                "content": content,
                "author": author,
                "timestamp": timestamp,
                "message_id": message_id,
                "channel_id": self.channel_id,
                "attachments": attachments,
                "reply_to": reply_data,
                "reactions": reactions
            }
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            raise

    async def verify_token(self) -> bool:
        if not self.token:
            return False
        try:
            async with self.session.get("https://discord.com/api/v9/users/@me", headers=self.headers) as response:
                return response.status == 200
        except:
            return False

    async def fetch_message_chunk(self, before_id: Optional[str] = None, limit: int = 100) -> list:
        params = {"limit": min(limit, 100)}
        if before_id:
            params["before"] = before_id

        try:
            if self.token:
                url = f"https://discord.com/api/v9/channels/{self.channel_id}/messages"
                async with self.session.get(url, headers=self.headers, params=params) as response:
                    if response.status == 200:
                        return await response.json()
                    if response.status != 403:  
                        raise Exception(f"Failed to fetch messages: {response.status}")

            channel = self.bot.get_channel(int(self.channel_id))
            if channel and hasattr(channel, 'guild') and channel.permissions_for(channel.guild.me).read_message_history:
                messages = await channel.history(
                    limit=limit, 
                    before=discord.Object(id=before_id) if before_id else None
                ).flatten()
                return messages
                
            raise Exception("No valid access method available")
                
        except Exception as e:
            logger.error(f"Error in fetch_message_chunk: {e}")
            return []

    async def fetch_messages(self, limit: int = 100) -> list:
        processed_messages = []
        last_id = None
        
        while len(processed_messages) < limit:
            chunk_size = min(100, limit - len(processed_messages))
            try:
                messages = await self.fetch_message_chunk(last_id, chunk_size)
                if not messages:
                    break
                
                for message in messages:
                    processed_message = await self.process_message(message)
                    processed_messages.append(processed_message)
                    
                if messages:
                    last_id = messages[-1].id if isinstance(messages[0], discord.Message) else messages[-1]["id"]
                
            except Exception as e:
                print(f"Error fetching messages: {e}")
                break
                
        return processed_messages

    async def fetch_reactions(self, message_id: str, message: Union[discord.Message, dict]) -> list:
        try:
            reactions = []
            if isinstance(message, discord.Message):
                for reaction in message.reactions:
                    users = []
                    async for user in reaction.users():
                        users.append(self.format_author(user))
                    reactions.append({
                        "emoji": str(reaction.emoji),
                        "count": reaction.count,
                        "users": users
                    })
            else:
                try:
                    async with self.session.get(
                        f"https://discord.com/api/v9/channels/{self.channel_id}/messages/{message_id}/reactions",
                        headers=self.headers
                    ) as response:
                        if response.status == 200:
                            reaction_data = await response.json()
                            for reaction in reaction_data:
                                users = [self.format_author(user) for user in reaction.get("users", [])]
                                reactions.append({
                                    "emoji": reaction.get("emoji", {}).get("name", ""),
                                    "count": reaction.get("count", 0),
                                    "users": users
                                })
                except Exception:
                    pass
            return reactions
        except Exception as e:
            logger.error(f"Error fetching reactions: {e}")
            return []

    @staticmethod
    async def process_message_static(bot, message: dict, session: Optional[aiohttp.ClientSession] = None) -> dict:
        """
        Static version of process_message for handling direct API responses
        """
        try:
            author = MessageRecorder.format_author_static(message["author"])
            content = message.get("content", "")
            timestamp = message["timestamp"]
            message_id = str(message["id"])
            channel_id = message.get("channel_id", "")
            
            attachments = [{
                "url": att["url"],
                "filename": att["filename"],
                "content_type": att.get("content_type", "unknown"),
                "size": att.get("size", 0)
            } for att in message.get("attachments", [])]
            
            reply_data = None
            if message.get("referenced_message"):
                ref_msg = message["referenced_message"]
                reply_data = {
                    "message_id": str(ref_msg["id"]),
                    "author": MessageRecorder.format_author_static(ref_msg["author"]),
                    "content": ref_msg.get("content", "")
                }

            reactions = await MessageRecorder.fetch_reactions_static(message_id, message, session)
            
            return {
                "content": content,
                "author": author,
                "timestamp": timestamp,
                "message_id": message_id,
                "channel_id": channel_id,
                "attachments": attachments,
                "reply_to": reply_data,
                "reactions": reactions
            }
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            raise

    @staticmethod
    def format_author_static(author: dict) -> dict:
        """
        Static version of format_author for handling API responses
        """
        return {
            "id": str(author.get("id")),
            "username": author.get("username", "Unknown"),
            "global_name": author.get("global_name"),
            "avatar_url": (f"https://cdn.discordapp.com/avatars/{author.get('id')}/{author.get('avatar')}.png" 
                         if author.get("avatar") else None)
        }

    @staticmethod
    async def fetch_reactions_static(message_id: str, message: dict, session: Optional[aiohttp.ClientSession]) -> list:
        """
        Static version of fetch_reactions for handling API responses
        """
        reactions = []
        if not session:
            return reactions
            
        try:
            if "reactions" in message:
                for reaction in message["reactions"]:
                    reactions.append({
                        "emoji": reaction.get("emoji", {}).get("name", ""),
                        "count": reaction.get("count", 0),
                        "users": []  
                    })
        except Exception as e:
            logger.error(f"Error fetching reactions: {e}")
        
        return reactions
