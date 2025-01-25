import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
import asyncio , uuid
from datetime import datetime, timezone
from discord.errors import HTTPException, Forbidden
from typing import Dict, List
from utils import DMTracker, TokenManager, MessageRecorder, logger

BACKEND_URL = "https://api-v9ww.onrender.com" #Update with yours or use mine
BOT_TOKEN = "UR TOKEN"
MAX_MESSAGES = 100000 #Max limit of messages to fetch

class ConversationBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.reactions = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.active_recordings = {}
        self.session = None
        self.token_manager = None
        self.dm_trackers: Dict[str, DMTracker] = {}
        self._setup_lock = asyncio.Lock()

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        self.token_manager = TokenManager()
        await self.token_manager.setup_database() 
        self.tree.copy_global_to(guild=discord.Object(id=0))
        await self.tree.sync()

    async def close(self):
        if self.session:
            await self.session.close()
        if self.token_manager:
            await self.token_manager.cleanup_old_tokens() 
        await super().close()

bot = ConversationBot()
bot.tree.allowed_contexts = app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True)
bot.tree.allowed_installs = app_commands.AppInstallationType(guild=True, user=True)

@bot.tree.command(name="autosavedms", description="Select DM channels to auto-save")
async def select_dms(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)
        
        user_id = str(interaction.user.id)
        token = await bot.token_manager.get_token(user_id)
        
        if not token:
            await interaction.followup.send(
                "Please set up your authorization token first using /auth",
                ephemeral=True
            )
            return
        if user_id in bot.dm_trackers:
            del bot.dm_trackers[user_id]

        headers = {"authorization": token}
        
        
        async with bot.session.get(
            "https://discord.com/api/v9/users/@me/channels",
            headers=headers
        ) as response:
            if response.status != 200:
                await interaction.followup.send("Failed to fetch DM channels", ephemeral=True)
                return
                
            channels = await response.json()
            dm_channels = [c for c in channels if c['type'] == 1]

            class DMSelectView(discord.ui.View):
                def __init__(self, dm_channels):
                    super().__init__()
                    self.current_page = 0
                    self.dm_channels = dm_channels
                    self.selected_channels = set()
                    self.update_select_menu()

                def update_select_menu(self):
                    
                    for item in self.children[:]:
                        if isinstance(item, discord.ui.Select):
                            self.remove_item(item)

                    
                    start_idx = self.current_page * 25
                    end_idx = min(start_idx + 25, len(self.dm_channels))
                    
                    
                    options = []
                    for channel in self.dm_channels[start_idx:end_idx]:
                        recipient = channel['recipients'][0]
                        options.append(
                            discord.SelectOption(
                                label=f"DM with {recipient['username']}",
                                value=channel['id'],
                                description=f"User ID: {recipient['id']}",
                                default=channel['id'] in self.selected_channels
                            )
                        )

                    select_menu = discord.ui.Select(
                        placeholder="Select DMs to auto-save",
                        min_values=0,
                        max_values=len(options),
                        options=options
                    )

                    async def select_callback(interaction: discord.Interaction):
                        self.selected_channels.update(select_menu.values)
                        await interaction.response.defer()

                    select_menu.callback = select_callback
                    self.add_item(select_menu)

                @discord.ui.button(label="Previous", style=discord.ButtonStyle.gray)
                async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
                    if self.current_page > 0:
                        self.current_page -= 1
                        self.update_select_menu()
                        await interaction.response.edit_message(view=self)

                @discord.ui.button(label="Next", style=discord.ButtonStyle.gray)
                async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
                    if (self.current_page + 1) * 25 < len(self.dm_channels):
                        self.current_page += 1
                        self.update_select_menu()
                        await interaction.response.edit_message(view=self)

                @discord.ui.button(label="Save Selection", style=discord.ButtonStyle.green)
                async def save_selection(self, interaction: discord.Interaction, button: discord.ui.Button):
                    if not self.selected_channels:
                        await interaction.response.send_message("Please select at least one DM channel", ephemeral=True)
                        return

                    
                    if user_id in bot.dm_trackers:
                        del bot.dm_trackers[user_id]
                    
                    
                    tracker = DMTracker(user_id, list(self.selected_channels))
                    bot.dm_trackers[user_id] = tracker
                    
                    
                    asyncio.create_task(monitor_dms(user_id, token, list(self.selected_channels)))
                    
                    await interaction.response.send_message(
                        f"Started auto-saving {len(self.selected_channels)} DM channels!",
                        ephemeral=True
                    )

            view = DMSelectView(dm_channels)
            await interaction.followup.send(
                "Select the DM channels you want to auto-save:",
                view=view,
                ephemeral=True
            )

    except Exception as e:
        logger.error(f"Error in select_dms: {e}")
        await interaction.followup.send(f"An error occurred: {str(e)}", ephemeral=True)

@bot.tree.command(name="removeautosavedms", description="Stop auto-saving DM channels")
async def remove_autosave(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)
        
        user_id = str(interaction.user.id)
        
        if user_id not in bot.dm_trackers:
            await interaction.followup.send(
                "You don't have any auto-saved DM channels!",
                ephemeral=True
            )
            return
        
        del bot.dm_trackers[user_id]
        
        await interaction.followup.send(
            "Successfully stopped auto-saving DM channels!",
            ephemeral=True
        )
            
    except Exception as e:
        logger.error(f"Error in remove_autosave: {e}")
        await interaction.followup.send(
            f"An error occurred: {str(e)}",
            ephemeral=True
        )

@bot.tree.command(name="showautosaves", description="View your auto-saved DMs")
async def show_auto_saves(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)
        
        user_id = str(interaction.user.id)
        if user_id not in bot.dm_trackers:
            await interaction.followup.send("No auto-saved DMs found!", ephemeral=True)
            return

        tracker = bot.dm_trackers[user_id]
        token = await bot.token_manager.get_token(user_id)
        headers = {"authorization": token}
        
        embed = discord.Embed(
            title="Auto-saved DMs",
            description="Here are your auto-saved DM conversations:",
            color=discord.Color.green()
        )

        for channel_id in tracker.channel_ids:
            try:
                async with bot.session.get(
                    f"https://discord.com/api/v9/channels/{channel_id}",
                    headers=headers
                ) as response:
                    if response.status == 200:
                        channel_data = await response.json()
                        username = channel_data['recipients'][0]['username']

                        new_conversation_id = str(uuid.uuid4())
                        tracker.conversation_ids[channel_id] = new_conversation_id
                        
                        conversation_data = {
                            "conversation_id": new_conversation_id,
                            "messages": list(tracker.messages[channel_id].values()) + 
                                      list(tracker.deleted_messages[channel_id].values()),
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "channel_id": f"dm_{channel_id}"
                        }

                        async with bot.session.post(
                            f"{BACKEND_URL}/conversations/",
                            json=conversation_data
                        ) as conv_response:
                            if conv_response.status == 200:
                                await asyncio.sleep(1)                    
                                share_url = None
                                retries = 3
                                while retries > 0 and not share_url:
                                    share_url = await get_share_url(
                                        new_conversation_id,
                                        bot.session
                                    )
                                    if not share_url:
                                        await asyncio.sleep(1)
                                        retries -= 1

                                if share_url:
                                    active_count = len(tracker.messages[channel_id])
                                    deleted_count = len(tracker.deleted_messages[channel_id])
                                    total_count = active_count + deleted_count
                                    
                                    embed.add_field(
                                        name=f"DM with {username}",
                                        value=(
                                            f"🔗 [View Conversation]({share_url})\n"
                                            f"📊 Statistics:\n"
                                            f"• Total messages: {total_count}\n"
                                            f"• Active messages: {active_count}\n"
                                            f"• Deleted messages: {deleted_count}\n"
                                            f"• Last updated: <t:{int(datetime.now(timezone.utc).timestamp())}:R>"
                                        ),
                                        inline=False
                                    )

            except Exception as e:
                logger.error(f"Error processing channel {channel_id}: {e}")
                continue

        if len(embed.fields) > 0:
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send("Failed to retrieve DM conversations.", ephemeral=True)

    except Exception as e:
        logger.error(f"Error in show_auto_saves: {e}")
        await interaction.followup.send(f"An error occurred: {str(e)}", ephemeral=True)


async def monitor_dms(user_id: str, token: str, channel_ids: List[str]):
    headers = {"authorization": token}
    
    logger.info(f"Starting DM monitoring for user {user_id} in channels: {channel_ids}")
    
    while user_id in bot.dm_trackers:
        try:
            tracker = bot.dm_trackers[user_id]
            
            for channel_id in channel_ids:
                async with bot.session.get(
                    f"https://discord.com/api/v9/channels/{channel_id}/messages",
                    headers=headers,
                    params={"limit": 50}
                ) as response:
                    if response.status == 200:
                        messages = await response.json()
                        current_message_ids = set()
                        
                        for message in messages:
                            message_id = message['id']
                            current_message_ids.add(message_id)
                            message_time = datetime.fromisoformat(
                                message['timestamp'].rstrip('Z')
                            ).replace(tzinfo=timezone.utc)
                            
                            if message_time >= tracker.start_time:
                                message['channel_id'] = channel_id
                                processed_message = await MessageRecorder.process_message_static(
                                    bot,
                                    message,
                                    bot.session
                                )
                                await tracker.add_message(processed_message)
                        
                        
                        cached_ids = tracker.message_cache[channel_id]
                        deleted_ids = cached_ids - current_message_ids
                        
                        for deleted_id in deleted_ids:
                            if deleted_id in tracker.messages[channel_id]:
                                await tracker.mark_as_deleted(deleted_id, channel_id)

            await asyncio.sleep(2)
            
        except Exception as e:
            logger.error(f"Error in monitor_dms: {e}")
            await asyncio.sleep(5)

async def check_deleted_messages(user_id: str, token: str):
    tracker = bot.dm_trackers.get(user_id)
    if not tracker:
        return

    headers = {"authorization": token}
    message_ids = list(tracker.messages.keys())
    
    async def check_message(message_id: str):
        try:
            channel_id = tracker.messages[message_id]['channel_id']
            async with bot.session.get(
                f"https://discord.com/api/v9/channels/{channel_id}/messages/{message_id}",
                headers=headers
            ) as response:
                if response.status == 404:
                    await tracker.mark_as_deleted(message_id)
        except Exception as e:
            logger.error(f"Error checking message {message_id}: {e}")

    
    batch_size = 5
    for i in range(0, len(message_ids), batch_size):
        batch = message_ids[i:i + batch_size]
        await asyncio.gather(*[check_message(mid) for mid in batch])

@bot.tree.command(name="save", description="Start recording messages in this channel")
async def start_recording(interaction: discord.Interaction):
    try:
        if interaction.channel_id in bot.active_recordings:
            await interaction.response.send_message("Already recording in this channel!", ephemeral=True)
            return

        channel = interaction.channel
        token = await bot.token_manager.get_token(str(interaction.user.id))
        
        
        needs_token = isinstance(channel, discord.DMChannel) or (
            isinstance(channel, discord.TextChannel) and 
            not channel.permissions_for(channel.guild.me).read_message_history
        )
        
        if needs_token and not token:
            await interaction.response.send_message(
                "Please set up your authorization token first using /auth to record messages",
                ephemeral=True
            )
            return

        recorder = MessageRecorder(bot,str(interaction.channel_id), token, bot.session)
        
        
        if token:
            is_valid = await recorder.verify_token()
            if not is_valid:
                await interaction.response.send_message(
                    "Your authorization token is invalid or expired. Please update it using /auth",
                    ephemeral=True
                )
                return

        bot.active_recordings[interaction.channel_id] = recorder
        await interaction.response.send_message(
            "📝 Started recording messages! Use /stop to end recording.", 
            ephemeral=True
        )
        
        
        if token:
            asyncio.create_task(periodic_message_fetch(interaction.channel_id))
        
    except Exception as e:
        logger.error(f"Error in start_recording: {e}")
        await interaction.response.send_message(f"❌ An error occurred: {str(e)}", ephemeral=True)

async def periodic_message_fetch(channel_id: int):
    """Periodically fetch new messages for token-based recording"""
    while channel_id in bot.active_recordings and bot.active_recordings[channel_id].is_recording:
        recorder = bot.active_recordings[channel_id]
        if recorder.use_token_based:
            await recorder.fetch_new_messages()
        await asyncio.sleep(2)  


@bot.tree.command(name="savehistory", description="Save message history from this channel")
async def save_history(interaction: discord.Interaction, message_limit: int = 100):
    try:
        await interaction.response.defer(ephemeral=True)
        
        if message_limit > MAX_MESSAGES:
            await interaction.followup.send(f"Message limit cannot exceed {MAX_MESSAGES}", ephemeral=True)
            return

        channel = interaction.channel
        token = await bot.token_manager.get_token(str(interaction.user.id))
        
        
        if isinstance(channel, discord.TextChannel):
            permissions = channel.permissions_for(channel.guild.me)
            if not permissions.read_message_history and not token:
                await interaction.followup.send(
                    "Bot doesn't have permission to read messages. Please either grant permissions or set up user token using /auth",
                    ephemeral=True
                )
                return

        
        if isinstance(channel, discord.DMChannel) and not token:
            await interaction.followup.send("Please set up your authorization token first using /auth", ephemeral=True)
            return

        recorder = MessageRecorder(bot,str(interaction.channel_id), token, bot.session)
        
        if token:
            is_valid = await recorder.verify_token()
            if not is_valid:
                await interaction.followup.send(
                    "Your authorization token is invalid or expired. Please update it using /settoken",
                    ephemeral=True
                )
                return

        messages = await recorder.fetch_messages(message_limit)
        
        if not messages:
            await interaction.followup.send("No messages found to archive!", ephemeral=True)
            return
            
        conversation_data = {
            "conversation_id": recorder.conversation_id,
            "messages": messages,
            "channel_id": str(interaction.channel_id),
            "created_at": recorder.start_time.isoformat()
        }

        async with bot.session.post(
            f"{BACKEND_URL}/conversations/",
            json=conversation_data,
            headers={"Content-Type": "application/json"}
        ) as response:
            if response.status == 200:
                share_url = await get_share_url(recorder.conversation_id, bot.session)
                if share_url:
                    embed = discord.Embed(
                        title="Messages Archived",
                        description=f"Messages have been archived successfully!\n [View them here]({share_url})",
                        color=discord.Color.green()
                    )
                    await interaction.followup.send(embed=embed, ephemeral=True)
                else:
                    await interaction.followup.send("Messages saved but couldn't generate share URL. Please try again later.", ephemeral=True)
            else:
                await interaction.followup.send(f"Failed to save messages. Status: {response.status}", ephemeral=True)
                
    except Exception as e:
        await interaction.followup.send(f"Error: {str(e)}", ephemeral=True)
        
@bot.tree.command(name="auth", description="Set up your authorization for message archiving")
async def setup_auth(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Discord Authorization Setup",
        description=(
            "**Desktop Instructions:**\n"
            "1. Open Discord in Chrome/Firefox\n"
            "2. Press F12 to open Developer Tools\n"
            "3. Go to the Network tab\n"
            "4. Type 'api' in the filter box\n"
            "5. Look for any request (like messages)\n"
            "6. Click on the request\n"
            "7. Find 'authorization' under Request Headers\n"
            "8. Copy your token\n\n"
            "**Mobile Instructions:**\n"
            "1. Open Discord in a mobile browser (Chrome/Safari)\n"
            "2. Request desktop site (in browser settings)\n"
            "3. Log into Discord\n"
            "4. Open browser developer tools (in browser settings)\n"
            "5. Follow steps 3-8 from desktop instructions\n\n"
            "Then use `/settoken your_token_here` to save it.\n\n"
            "⚠️ Keep your token private! Never share it with anyone."
        ),
        color=discord.Color.blue()
    )
    embed.add_field(
        name="Why is this needed?",
        value="Your token allows the bot to archive messages from DMs and servers where it isn't present.",
        inline=False
    )
    embed.add_field(
        name="Security Note",
        value="Your token is encrypted before storage and can only be used for reading messages.",
        inline=False
    )
    embed.add_field(
        name="Open Source",
        value="(This project is completely open source! You can review the code and see exactly how the bot works at:)[https://github.com/flyhighr/Archiver/]",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="settoken", description="Set your Discord authorization token")
async def set_token(interaction: discord.Interaction, token: str):
    await interaction.response.defer(ephemeral=True)
    try:
        
        async with aiohttp.ClientSession() as session:
            headers = {"authorization": token}
            async with session.get("https://discord.com/api/v9/users/@me", headers=headers) as response:
                if response.status != 200:
                    await interaction.followup.send("❌ Invalid token provided!", ephemeral=True)
                    return
                
                user_data = await response.json()
                if str(user_data['id']) != str(interaction.user.id):
                    await interaction.followup.send("❌ This token doesn't belong to your account!", ephemeral=True)
                    return
        
        
        await bot.token_manager.store_token(str(interaction.user.id), token)
        await interaction.followup.send("✅ Token verified and stored securely!", ephemeral=True)
    except Exception as e:
        logger.error(f"Error in set_token: {e}")
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)



@bot.tree.command(name="deletetoken", description="Remove your stored Discord authorization token")
async def remove_token(interaction: discord.Interaction):
    try:
        await bot.token_manager.delete_token(str(interaction.user.id))
        await interaction.response.send_message("✅ Token removed successfully!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="stop", description="Stop recording and save the messages")
async def stop_recording(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)
        
        if interaction.channel_id not in bot.active_recordings:
            await interaction.followup.send("No active recording in this channel!", ephemeral=True)
            return

        recorder = bot.active_recordings[interaction.channel_id]
        recorder.is_recording = False

        if not recorder.messages:
            await interaction.followup.send("❌ No messages were recorded!", ephemeral=True)
            del bot.active_recordings[interaction.channel_id]
            return

        conversation_data = {
            "conversation_id": recorder.conversation_id,
            "messages": recorder.messages,
            "created_at": recorder.start_time.isoformat(),
            "channel_id": str(recorder.channel_id)
        }

        try:
            async with bot.session.post(
                f"{BACKEND_URL}/conversations/",
                json=conversation_data
            ) as response:
                if response.status == 200:
                    await interaction.followup.send("✅ Recording stopped! Processing conversation...", ephemeral=True)
                    asyncio.create_task(
                        wait_for_share_url(interaction.user, interaction.channel, recorder.conversation_id)
                    )
                else:
                    await interaction.followup.send(f"❌ Error saving conversation! Status: {response.status}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error saving conversation: {str(e)}", ephemeral=True)

        del bot.active_recordings[interaction.channel_id]
    except Exception as e:
        await interaction.followup.send(f"❌ An error occurred: {str(e)}", ephemeral=True)

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.channel.id in bot.active_recordings:
        recorder = bot.active_recordings[message.channel.id]
        if recorder.is_recording:
            await recorder.record_message(message)

    await bot.process_commands(message)

@bot.event
async def on_reaction_add(reaction, user):
    if user == bot.user:
        return

    await handle_reaction_change(reaction.message)

@bot.event
async def on_reaction_remove(reaction, user):
    if user == bot.user:
        return

    await handle_reaction_change(reaction.message)

async def handle_reaction_change(message):
    if message.channel.id in bot.active_recordings:
        recorder = bot.active_recordings[message.channel.id]
        if recorder.is_recording:
            message_time = message.created_at.replace(tzinfo=timezone.utc)
            if message_time >= recorder.start_time:
                for idx, msg in enumerate(recorder.messages):
                    if msg["message_id"] == str(message.id):
                        try:
                            processed_message = await recorder.process_message(message)
                            recorder.messages[idx] = processed_message
                            break
                        except Exception as e:
                            print(f"Error updating message reactions: {e}")

async def get_share_url(conversation_id: str, session: aiohttp.ClientSession) -> str:
    try:
        async with session.get(f"{BACKEND_URL}/conversations/{conversation_id}/share-url") as response:
            if response.status == 200:
                data = await response.json()
                return data.get('share_url')
            print(f"Share URL fetch failed with status {response.status}")
            return None
    except Exception as e:
        print(f"Share URL fetch error: {str(e)}")
        return None

async def wait_for_share_url(user: discord.User, channel: discord.abc.Messageable, conversation_id: str):
    retries = 0
    max_retries = 12
    backoff_time = 10

    while retries < max_retries:
        try:
            async with bot.session.get(
                f"{BACKEND_URL}/conversations/{conversation_id}/share-url",
                timeout=30
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    try:
                        embed = discord.Embed(title="Here's Your conversation archive",description=f"[click here]({data['share_url']})",color=discord.Color.green())
                        await user.send(embed=embed)
                        return
                    except (HTTPException, Forbidden):
                        await channel.send(
                            f"{user.mention} Here's your conversation archive: {data['share_url']}",
                            delete_after=30
                        )
                        
                        return
        except Exception as e:
            print(f"Error fetching share URL (attempt {retries + 1}/{max_retries}): {str(e)}")
        
        retries += 1
        await asyncio.sleep(backoff_time)
        backoff_time = min(backoff_time * 1.5, 60)

    try:
        await user.send("❌ Unable to generate share URL. Please try recording again later.")
    except (HTTPException, Forbidden):
        await channel.send(
            f"{user.mention} ❌ Unable to generate share URL. Please try recording again later.",
            delete_after=30
        )

if __name__ == "__main__":
    bot.run(BOT_TOKEN)
