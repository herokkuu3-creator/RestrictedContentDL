# Copyright (C) @TheSmartBisnu
# Channel: https://t.me/itsSmartDev

import os
import shutil
import psutil
import asyncio
from time import time
from aiohttp import web

from pyleaves import Leaves
from pyrogram.enums import ParseMode
from pyrogram import Client, filters
from pyrogram.errors import PeerIdInvalid, BadRequest
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from helpers.utils import (
    processMediaGroup,
    progressArgs,
    send_media
)

from helpers.files import (
    get_download_path,
    fileSizeLimit,
    get_readable_file_size,
    get_readable_time,
    cleanup_download
)

from helpers.msg import (
    getChatMsgID,
    get_file_name,
    get_parsed_msg
)

from config import PyroConf
from logger import LOGGER

# Initialize the bot client
bot = Client(
    "media_bot",
    api_id=PyroConf.API_ID,
    api_hash=PyroConf.API_HASH,
    bot_token=PyroConf.BOT_TOKEN,
    workers=100,
    parse_mode=ParseMode.MARKDOWN,
    max_concurrent_transmissions=1,
    sleep_threshold=30,
)

# Client for user session
user = Client(
    "user_session",
    workers=100,
    session_string=PyroConf.SESSION_STRING,
    max_concurrent_transmissions=1,
    sleep_threshold=30,
)

RUNNING_TASKS = set()
download_semaphore = None
BATCH_STATES = {}  # Stores state for user interactions: {user_id: {'step': '...', 'data': ...}}

def track_task(coro):
    task = asyncio.create_task(coro)
    RUNNING_TASKS.add(task)
    def _remove(_):
        RUNNING_TASKS.discard(task)
    task.add_done_callback(_remove)
    return task


@bot.on_message(filters.command("start") & filters.private)
async def start(_, message: Message):
    welcome_text = (
        "👋 **Welcome to Media Downloader Bot!**\n\n"
        "I can grab photos, videos, audio, and documents from any Telegram post.\n"
        "Just send me a link (paste it directly or use `/dl <link>`),\n"
        "or reply to a message with `/dl`.\n\n"
        "**New Feature:**\n"
        "Use `/batch` to clone/download multiple messages easily!\n\n"
        "ℹ️ Use `/help` to view all commands and examples.\n"
        "🔒 Make sure the user client is part of the chat.\n\n"
        "Ready? Send me a Telegram post link!"
    )

    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Update Channel", url="https://t.me/itsSmartDev")]]
    )
    await message.reply(welcome_text, reply_markup=markup, disable_web_page_preview=True)


@bot.on_message(filters.command("help") & filters.private)
async def help_command(_, message: Message):
    help_text = (
        "💡 **Media Downloader Bot Help**\n\n"
        "➤ **Single Download**\n"
        "   – Just paste a link or use `/dl <link>`.\n\n"
        "➤ **Batch Process (Simple)**\n"
        "   1. Send `/batch`\n"
        "   2. Send the **Start Link**\n"
        "   3. Send the **Number of Messages** (e.g., 100)\n"
        "   The bot will calculate the range and process them.\n\n"
        "➤ **Requirements**\n"
        "   – Make sure the user client is part of the chat.\n\n"
        "➤ **Management**\n"
        "   – `/killall` : Cancel all running tasks.\n"
        "   – `/logs` : Get log file.\n"
        "   – `/stats` : System status.\n"
    )
    
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Update Channel", url="https://t.me/itsSmartDev")]]
    )
    await message.reply(help_text, reply_markup=markup, disable_web_page_preview=True)


# -------------------------------------------------------------------------------------
# CORE DOWNLOAD LOGIC (With Cloning)
# -------------------------------------------------------------------------------------
async def handle_download(bot: Client, message: Message, post_url: str):
    async with download_semaphore:
        if "?" in post_url:
            post_url = post_url.split("?", 1)[0]

        try:
            chat_id, message_id = getChatMsgID(post_url)
            chat_message = await user.get_messages(chat_id=chat_id, message_ids=message_id)
            
            LOGGER(__name__).info(f"Processing URL: {post_url}")

            # --- 1. TRY DIRECT CLONE (Optimization) ---
            try:
                # Attempt to copy message directly. Fails if restricted or privacy blocks it.
                if chat_message.media_group_id:
                    await user.copy_media_group(
                        chat_id=message.chat.id, 
                        from_chat_id=chat_id, 
                        message_id=message_id
                    )
                else:
                    await user.copy_message(
                        chat_id=message.chat.id, 
                        from_chat_id=chat_id, 
                        message_id=message_id
                    )
                
                # If success, wait a bit and return (skip download)
                LOGGER(__name__).info(f"Directly cloned message from {post_url}")
                # Using FLOOD_WAIT_DELAY as the standard delay between actions
                await asyncio.sleep(PyroConf.FLOOD_WAIT_DELAY)
                return 

            except Exception as e:
                # Clone failed (Restricted content?), falling back to download
                LOGGER(__name__).info(f"Direct clone failed for {post_url}, falling back to download. Reason: {e}")
            # ------------------------------------------

            # --- 2. FALLBACK: DOWNLOAD & UPLOAD ---
            if chat_message.document or chat_message.video or chat_message.audio:
                file_size = (
                    chat_message.document.file_size
                    if chat_message.document
                    else chat_message.video.file_size
                    if chat_message.video
                    else chat_message.audio.file_size
                )

                if not await fileSizeLimit(
                    file_size, message, "download", user.me.is_premium
                ):
                    return

            parsed_caption = await get_parsed_msg(
                chat_message.caption or "", chat_message.caption_entities
            )
            parsed_text = await get_parsed_msg(
                chat_message.text or "", chat_message.entities
            )

            if chat_message.media_group_id:
                if not await processMediaGroup(chat_message, bot, message):
                    await message.reply(
                        "**Could not extract any valid media from the media group.**"
                    )
                return

            elif chat_message.media:
                start_time = time()
                progress_message = await message.reply(f"**📥 Downloading {message_id}...**")

                filename = get_file_name(message_id, chat_message)
                download_path = get_download_path(message.id, filename)

                media_path = await chat_message.download(
                    file_name=download_path,
                    progress=Leaves.progress_for_pyrogram,
                    progress_args=progressArgs(
                        "📥 Downloading Progress", progress_message, start_time
                    ),
                )

                if not media_path or not os.path.exists(media_path):
                    await progress_message.edit("**❌ Download failed: File not saved properly**")
                    return

                file_size = os.path.getsize(media_path)
                if file_size == 0:
                    await progress_message.edit("**❌ Download failed: File is empty**")
                    cleanup_download(media_path)
                    return

                LOGGER(__name__).info(f"Downloaded media: {media_path} (Size: {file_size} bytes)")

                media_type = (
                    "photo"
                    if chat_message.photo
                    else "video"
                    if chat_message.video
                    else "audio"
                    if chat_message.audio
                    else "document"
                )
                await send_media(
                    bot,
                    message,
                    media_path,
                    media_type,
                    parsed_caption,
                    progress_message,
                    start_time,
                )

                cleanup_download(media_path)
                await progress_message.delete()

            elif chat_message.text or chat_message.caption:
                await message.reply(parsed_text or parsed_caption)
            else:
                await message.reply("**No media or text found in the post URL.**")

        except (PeerIdInvalid, BadRequest, KeyError):
            await message.reply(f"**Error processing {post_url}: User client likely not in chat.**")
        except Exception as e:
            error_message = f"**❌ Error at {post_url}: {str(e)}**"
            await message.reply(error_message)
            LOGGER(__name__).error(e)


@bot.on_message(filters.command("dl") & filters.private)
async def download_media(bot: Client, message: Message):
    if len(message.command) < 2:
        await message.reply("**Provide a post URL after the /dl command.**")
        return
    post_url = message.command[1]
    await track_task(handle_download(bot, message, post_url))


# -------------------------------------------------------------------------------------
# NEW /BATCH INTERACTIVE FLOW
# -------------------------------------------------------------------------------------
@bot.on_message(filters.command("batch") & filters.private)
async def batch_command_start(bot: Client, message: Message):
    # Set initial state
    BATCH_STATES[message.from_user.id] = {'step': 'ask_link'}
    await message.reply(
        "🚀 **Batch Mode Initiated**\n\n"
        "Please send the **Start Link** of the first post you want to download."
    )


# Generic Text Handler (Handles both single links AND batch conversation steps)
@bot.on_message(filters.private & ~filters.command(["start", "help", "dl", "batch", "stats", "logs", "killall"]))
async def handle_text_and_states(bot: Client, message: Message):
    # 1. Check if user is in a Batch conversation
    user_id = message.from_user.id
    state = BATCH_STATES.get(user_id)

    if state:
        # --- Step 1: User sent the Link ---
        if state['step'] == 'ask_link':
            if not message.text.startswith("https://t.me/"):
                await message.reply("❌ Invalid link. Please send a valid Telegram post link (e.g., https://t.me/channel/100).")
                return
            
            # Store link and move to next step
            BATCH_STATES[user_id]['start_link'] = message.text
            BATCH_STATES[user_id]['step'] = 'ask_count'
            await message.reply(
                "✅ Link accepted.\n\n"
                "**How many messages** do you want to process starting from there?\n"
                "(Send a number, e.g., `100`)"
            )
            return

        # --- Step 2: User sent the Count ---
        elif state['step'] == 'ask_count':
            if not message.text.isdigit():
                await message.reply("❌ Please send a valid number.")
                return
            
            count = int(message.text)
            start_link = BATCH_STATES[user_id]['start_link']
            
            # Clean up state
            del BATCH_STATES[user_id]
            
            # Execute Batch
            await execute_batch_logic(bot, message, start_link, count)
            return

    # 2. If not in state, treat as a single download link (if it looks like a link)
    if message.text and not message.text.startswith("/"):
        await track_task(handle_download(bot, message, message.text))


# Helper to run the batch loop
async def execute_batch_logic(bot: Client, message: Message, start_link: str, count: int):
    try:
        start_chat, start_id = getChatMsgID(start_link)
    except Exception as e:
        return await message.reply(f"**❌ Error parsing start link:\n{e}**")

    # Calculate End ID
    end_id = start_id + count - 1
    
    prefix = start_link.rsplit("/", 1)[0]
    
    loading = await message.reply(
        f"📥 **Starting Batch Process**\n"
        f"From: `{start_id}`\n"
        f"To: `{end_id}`\n"
        f"Total: `{count}` posts"
    )

    downloaded = skipped = failed = 0
    batch_tasks = []
    BATCH_SIZE = PyroConf.BATCH_SIZE

    for msg_id in range(start_id, end_id + 1):
        url = f"{prefix}/{msg_id}"
        try:
            # Check if message exists/is empty
            chat_msg = await user.get_messages(chat_id=start_chat, message_ids=msg_id)
            if not chat_msg:
                skipped += 1
                continue

            has_media = bool(chat_msg.media_group_id or chat_msg.media)
            has_text  = bool(chat_msg.text or chat_msg.caption)
            if not (has_media or has_text):
                skipped += 1
                continue

            # Spawn task
            task = track_task(handle_download(bot, message, url))
            batch_tasks.append(task)

            # Wait if batch size reached
            if len(batch_tasks) >= BATCH_SIZE:
                results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, asyncio.CancelledError):
                        await loading.delete()
                        return await message.reply(
                            f"**❌ Batch canceled** after processing `{downloaded}` posts."
                        )
                    elif isinstance(result, Exception):
                        failed += 1
                        LOGGER(__name__).error(f"Error: {result}")
                    else:
                        downloaded += 1

                batch_tasks.clear()
                # Flood wait to be safe
                await asyncio.sleep(PyroConf.FLOOD_WAIT_DELAY)

        except Exception as e:
            failed += 1
            LOGGER(__name__).error(f"Error at {url}: {e}")

    # Process remaining tasks
    if batch_tasks:
        results = await asyncio.gather(*batch_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                failed += 1
            else:
                downloaded += 1

    await loading.delete()
    await message.reply(
        "**✅ Batch Process Complete!**\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"📥 **Processed** : `{downloaded}`\n"
        f"⏭️ **Skipped** : `{skipped}`\n"
        f"❌ **Failed** : `{failed}`"
    )


@bot.on_message(filters.command("stats") & filters.private)
async def stats(_, message: Message):
    currentTime = get_readable_time(time() - PyroConf.BOT_START_TIME)
    total, used, free = shutil.disk_usage(".")
    total = get_readable_file_size(total)
    used = get_readable_file_size(used)
    free = get_readable_file_size(free)
    sent = get_readable_file_size(psutil.net_io_counters().bytes_sent)
    recv = get_readable_file_size(psutil.net_io_counters().bytes_recv)
    
    stats_msg = (
        "**Bot Status**\n\n"
        f"**➜ Uptime:** `{currentTime}`\n"
        f"**➜ Disk Free:** `{free}`\n"
        f"**➜ Upload:** `{sent}`\n"
        f"**➜ Download:** `{recv}`"
    )
    await message.reply(stats_msg)


@bot.on_message(filters.command("logs") & filters.private)
async def logs(_, message: Message):
    if os.path.exists("logs.txt"):
        await message.reply_document(document="logs.txt", caption="**Logs**")
    else:
        await message.reply("**Not exists**")


@bot.on_message(filters.command("killall") & filters.private)
async def cancel_all_tasks(_, message: Message):
    cancelled = 0
    # Clear state if any
    if message.from_user.id in BATCH_STATES:
        del BATCH_STATES[message.from_user.id]
        
    for task in list(RUNNING_TASKS):
        if not task.done():
            task.cancel()
            cancelled += 1
    await message.reply(f"**Cancelled {cancelled} running task(s).**")


async def initialize():
    global download_semaphore
    download_semaphore = asyncio.Semaphore(PyroConf.MAX_CONCURRENT_DOWNLOADS)


# -------------------------------------------------------------------------------------
# Dummy Web Server for Render
# -------------------------------------------------------------------------------------
async def web_server():
    async def handle(request):
        return web.Response(text="Bot is running!")

    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.getenv('PORT', 8080)))
    await site.start()
    LOGGER(__name__).info(f"Web server started on port {os.getenv('PORT', 8080)}")


# -------------------------------------------------------------------------------------
# MAIN EXECUTION
# -------------------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        LOGGER(__name__).info("Bot Started!")
        loop = asyncio.get_event_loop()
        
        # Initialize semaphore
        loop.run_until_complete(initialize())
        
        # Start the User Client
        # FIX: Directly call start() because it is synchronous in this library version.
        user.start()
        
        # Start the Dummy Web Server
        loop.run_until_complete(web_server())
        
        # Start the Bot Client
        bot.run()
        
    except KeyboardInterrupt:
        pass
    except Exception as err:
        LOGGER(__name__).error(err)
    finally:
        LOGGER(__name__).info("Bot Stopped")
