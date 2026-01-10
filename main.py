# Copyright (C) @TheSmartBisnu
# Channel: https://t.me/itsSmartDev

import os
import shutil
import psutil
import asyncio
from time import time
from aiohttp import web

from pyrogram.enums import ParseMode
from pyrogram import Client, filters
from pyrogram.errors import PeerIdInvalid, BadRequest, FloodWait, RPCError
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton

from helpers.utils import (
    processMediaGroup,
    send_media,
    progress_bar
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
BATCH_STATES = {}  

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
        "**New Feature:**\n"
        "Use `/batch` to clone/download multiple messages easily!\n"
        f"⚡ Parallel processing: **{PyroConf.MAX_CONCURRENT_DOWNLOADS} files at once**\n"
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
        "   3. Send the **Number of Messages**\n"
        "   The bot will process 3 files at a time.\n\n"
        "➤ **Management**\n"
        "   – `/killall` : Cancel all running tasks.\n"
        "   – `/logs` : Get log file.\n"
        "   – `/stats` : System status.\n"
    )
    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Update Channel", url="https://t.me/itsSmartDev")]]
    )
    await message.reply(help_text, reply_markup=markup, disable_web_page_preview=True)


async def handle_download(bot: Client, message: Message, post_url: str):
    # SEMAPHORE: Controls Parallelism (e.g., only 3 tasks enter here at once)
    async with download_semaphore:
        if "?" in post_url:
            post_url = post_url.split("?", 1)[0]

        try:
            chat_id, message_id = getChatMsgID(post_url)
            chat_message = await user.get_messages(chat_id=chat_id, message_ids=message_id)
            
            LOGGER(__name__).info(f"Processing URL: {post_url}")

            # --- 1. TRY DIRECT CLONE (Optimization) ---
            try:
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
                
                LOGGER(__name__).info(f"Directly cloned message from {post_url}")
                await asyncio.sleep(PyroConf.FLOOD_WAIT_DELAY)
                return 

            except FloodWait as fw:
                LOGGER(__name__).warning(f"FloodWait of {fw.value}s during clone. Sleeping...")
                await asyncio.sleep(fw.value)
            
            except RPCError as e:
                LOGGER(__name__).info(f"Direct clone not allowed, switching to download. Info: {e}")
                if "/batch" not in (message.text or ""):
                     temp_msg = await message.reply("⚠️ **Direct copy restricted. Downloading manually...**")
                     asyncio.create_task(delete_after(temp_msg, 5))

            except Exception as e:
                LOGGER(__name__).warning(f"Generic error during clone: {e}")

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

            if chat_message.media:
                start_time = time()
                # Create a UNIQUE progress message for this task
                progress_message = await message.reply(f"**📥 Downloading {message_id}...**")

                filename = get_file_name(message_id, chat_message)
                download_path = get_download_path(message.id, filename)

                # Download Interval: 20s
                DOWNLOAD_INTERVAL = 20

                media_path = await chat_message.download(
                    file_name=download_path,
                    progress=progress_bar,
                    progress_args=(progress_message, start_time, "📥 Downloading", DOWNLOAD_INTERVAL),
                )

                if not media_path or not os.path.exists(media_path):
                    await progress_message.edit("**❌ Download failed: File not saved properly**")
                    return

                file_size = os.path.getsize(media_path)
                if file_size == 0:
                    await progress_message.edit("**❌ Download failed: File is empty**")
                    cleanup_download(media_path)
                    return

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

async def delete_after(message, delay):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except:
        pass


@bot.on_message(filters.command("dl") & filters.private)
async def download_media(bot: Client, message: Message):
    if len(message.command) < 2:
        await message.reply("**Provide a post URL after the /dl command.**")
        return
    post_url = message.command[1]
    await track_task(handle_download(bot, message, post_url))


@bot.on_message(filters.command("batch") & filters.private)
async def batch_command_start(bot: Client, message: Message):
    BATCH_STATES[message.from_user.id] = {'step': 'ask_link'}
    await message.reply("🚀 **Batch Mode**\nPlease send the **Start Link**.")


@bot.on_message(filters.private & ~filters.command(["start", "help", "dl", "batch", "stats", "logs", "killall"]))
async def handle_text_and_states(bot: Client, message: Message):
    user_id = message.from_user.id
    state = BATCH_STATES.get(user_id)

    if state:
        if state['step'] == 'ask_link':
            if not message.text.startswith("https://t.me/"):
                await message.reply("❌ Invalid link.")
                return
            
            BATCH_STATES[user_id]['start_link'] = message.text
            BATCH_STATES[user_id]['step'] = 'ask_count'
            await message.reply("✅ Link accepted.\n**How many messages?** (e.g., `100`)")
            return

        elif state['step'] == 'ask_count':
            if not message.text.isdigit():
                await message.reply("❌ Please send a number.")
                return
            
            count = int(message.text)
            start_link = BATCH_STATES[user_id]['start_link']
            del BATCH_STATES[user_id]
            
            await execute_batch_logic(bot, message, start_link, count)
            return

    if message.text and not message.text.startswith("/"):
        await track_task(handle_download(bot, message, message.text))


async def execute_batch_logic(bot: Client, message: Message, start_link: str, count: int):
    try:
        start_chat, start_id = getChatMsgID(start_link)
    except Exception as e:
        return await message.reply(f"**❌ Error parsing start link:\n{e}**")

    end_id = start_id + count - 1
    prefix = start_link.rsplit("/", 1)[0]
    
    loading = await message.reply(
        f"📥 **Batch Started**\nRange: `{start_id}` to `{end_id}`\n"
        f"Parallel Downloads: `{PyroConf.MAX_CONCURRENT_DOWNLOADS}`"
    )

    downloaded = skipped = failed = 0
    batch_tasks = []
    # BATCH_SIZE is chunk size; Semaphore controls active downloads
    BATCH_SIZE = PyroConf.BATCH_SIZE 

    for msg_id in range(start_id, end_id + 1):
        url = f"{prefix}/{msg_id}"
        try:
            # We create a task for every message in sequence
            task = track_task(handle_download(bot, message, url))
            batch_tasks.append(task)

            # Process in chunks to avoid memory overload
            if len(batch_tasks) >= BATCH_SIZE:
                results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        failed += 1
                        LOGGER(__name__).error(f"Batch Error: {result}")
                    else:
                        downloaded += 1

                batch_tasks.clear()
                await asyncio.sleep(PyroConf.FLOOD_WAIT_DELAY)

        except Exception as e:
            failed += 1

    if batch_tasks:
        await asyncio.gather(*batch_tasks, return_exceptions=True)
        downloaded += len(batch_tasks)

    await loading.delete()
    await message.reply("**✅ Batch Process Complete!**")


async def initialize():
    global download_semaphore
    # This ensures only 3 downloads run at once
    download_semaphore = asyncio.Semaphore(PyroConf.MAX_CONCURRENT_DOWNLOADS)


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


if __name__ == "__main__":
    try:
        LOGGER(__name__).info("Bot Started!")
        loop = asyncio.get_event_loop()
        loop.run_until_complete(initialize())
        user.start()
        loop.run_until_complete(web_server())
        bot.run()
    except KeyboardInterrupt:
        pass
    except Exception as err:
        LOGGER(__name__).error(err)
    finally:
        LOGGER(__name__).info("Bot Stopped")
