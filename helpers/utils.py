# Copyright (C) @TheSmartBisnu
# Channel: https://t.me/itsSmartDev

import os
import asyncio
import time
import math
from typing import Optional
from asyncio.subprocess import PIPE
from asyncio import create_subprocess_exec, create_subprocess_shell, wait_for

# Removed Pyleaves import
# from pyleaves import Leaves 

from pyrogram.parser import Parser
from pyrogram.utils import get_channel_id
from pyrogram.types import (
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio,
    Voice,
)
from pyrogram.errors import MessageNotModified

from helpers.files import (
    fileSizeLimit,
    cleanup_download,
    get_readable_file_size, # Added import
    get_readable_time       # Added import
)

from helpers.msg import (
    get_parsed_msg
)

from logger import LOGGER

# Progress bar template
PROGRESS_BAR = """
Percentage: {percentage:.2f}% | {current}/{total}
Speed: {speed}/s
Estimated Time Left: {est_time}
"""

# Global cache to track the last update time for each message
# Format: {message_id: last_update_timestamp}
PROGRESS_CACHE = {}

async def cmd_exec(cmd, shell=False):
    if shell:
        proc = await create_subprocess_shell(cmd, stdout=PIPE, stderr=PIPE)
    else:
        proc = await create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
    stdout, stderr = await proc.communicate()
    try:
        stdout = stdout.decode().strip()
    except:
        stdout = "Unable to decode the response!"
    try:
        stderr = stderr.decode().strip()
    except:
        stderr = "Unable to decode the error!"
    return stdout, stderr, proc.returncode


async def get_media_info(path):
    try:
        result = await cmd_exec([
            "ffprobe", "-hide_banner", "-loglevel", "error",
            "-print_format", "json", "-show_format", "-show_streams", path,
        ])
    except Exception as e:
        LOGGER(__name__).error(f"Get Media Info: {e}. File: {path}")
        return 0, None, None, None, None

    if result[0] and result[2] == 0:
        try:
            import json
            data = json.loads(result[0])

            fields = data.get("format", {})
            duration = round(float(fields.get("duration", 0)))

            tags = fields.get("tags", {})
            artist = tags.get("artist") or tags.get("ARTIST") or tags.get("Artist")
            title = tags.get("title") or tags.get("TITLE") or tags.get("Title")

            width = None
            height = None
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video":
                    width = stream.get("width")
                    height = stream.get("height")
                    break

            return duration, artist, title, width, height
        except Exception as e:
            LOGGER(__name__).error(f"Error parsing media info: {e}")
            return 0, None, None, None, None
    return 0, None, None, None, None


async def get_video_thumbnail(video_file, duration):
    os.makedirs("Assets", exist_ok=True)
    output = os.path.join("Assets", "video_thumb.jpg")

    if duration is None:
        duration = (await get_media_info(video_file))[0]
    if not duration:
        duration = 3
    duration //= 2

    if os.path.exists(output):
        try:
            os.remove(output)
        except:
            pass

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", str(duration), "-i", video_file,
        "-vframes", "1", "-q:v", "2",
        "-y", output,
    ]
    try:
        _, err, code = await wait_for(cmd_exec(cmd), timeout=60)
        if code != 0 or not os.path.exists(output):
            LOGGER(__name__).warning(f"Thumbnail generation failed: {err}")
            return None
    except Exception as e:
        LOGGER(__name__).warning(f"Thumbnail generation error: {e}")
        return None
    return output


# Generate progress bar for downloading/uploading
def progressArgs(action: str, progress_message, start_time):
    # We pass the same args, but our custom function will use them differently
    return (action, progress_message, start_time, PROGRESS_BAR, "▓", "░")


# --- NEW CUSTOM PROGRESS FUNCTION ---
async def progress_for_pyrogram(current, total, action, message, start_time, template, finish, unfinish):
    now = time.time()
    
    # 1. Logic for Update Interval
    is_download = "Download" in action
    size_mb = total / (1024 * 1024)
    
    if is_download:
        # If < 500MB update every 20s, else 25s
        interval = 20 if size_mb < 500 else 25
    else:
        # If < 300MB update every 5s, else 9s
        interval = 5 if size_mb < 300 else 9

    last_update = PROGRESS_CACHE.get(message.id, 0)
    
    # Check if we should update (always update if finished, otherwise check interval)
    if current != total and (now - last_update) < interval:
        return

    # Update cache
    PROGRESS_CACHE[message.id] = now
    
    # 2. Calculate Stats
    percentage = (current * 100) / total
    
    # Use Average Speed (Total Bytes / Total Time) for stable ETL
    elapsed_time = now - start_time
    if elapsed_time <= 0: elapsed_time = 0.1 # avoid div by zero
    
    speed = current / elapsed_time
    speed_text = f"{get_readable_file_size(speed)}/s"
    
    remaining_bytes = total - current
    if speed > 0:
        etl_seconds = remaining_bytes / speed
        etl_text = get_readable_time(int(etl_seconds))
    else:
        etl_text = "0s"
    
    # 3. Generate Bar
    bar_len = 20
    filled = int(percentage / 100 * bar_len)
    bar = finish * filled + unfinish * (bar_len - filled)
    
    current_size = get_readable_file_size(current)
    total_size = get_readable_file_size(total)
    
    # Format Text
    text = template.format(
        percentage=percentage,
        current=current_size,
        total=total_size,
        speed=speed_text,
        est_time=etl_text
    )
    
    # 4. Edit Message
    try:
        await message.edit(f"**{action}**\n{bar}\n{text}")
    except MessageNotModified:
        pass
    except Exception as e:
        LOGGER(__name__).error(f"Progress Error: {e}")
        
    # Cleanup cache if finished
    if current == total:
        if message.id in PROGRESS_CACHE:
            del PROGRESS_CACHE[message.id]


async def send_media(
    bot, message, media_path, media_type, caption, progress_message, start_time, destination_chat_id=None
):
    file_size = os.path.getsize(media_path)

    # Use destination ID if provided, otherwise default to the chat command came from
    target_chat_id = destination_chat_id if destination_chat_id else message.chat.id

    if not await fileSizeLimit(file_size, message, "upload"):
        return

    progress_args = progressArgs("📥 Uploading Progress", progress_message, start_time)
    LOGGER(__name__).info(f"Uploading media: {media_path} ({media_type}) to {target_chat_id}")

    # Note: We use bot.send_* methods instead of message.reply_* to support custom destinations
    if media_type == "photo":
        await bot.send_photo(
            chat_id=target_chat_id,
            photo=media_path,
            caption=caption or "",
            progress=progress_for_pyrogram, # UPDATED
            progress_args=progress_args,
        )
    elif media_type == "video":
        duration, _, _, width, height = await get_media_info(media_path)

        if not duration or duration == 0:
            duration = 0
            LOGGER(__name__).warning(f"Could not extract duration for {media_path}")

        if not width or not height:
            width = 640
            height = 480

        thumb = await get_video_thumbnail(media_path, duration)

        await bot.send_video(
            chat_id=target_chat_id,
            video=media_path,
            duration=duration,
            width=width,
            height=height,
            thumb=thumb,
            caption=caption or "",
            supports_streaming=True,
            progress=progress_for_pyrogram, # UPDATED
            progress_args=progress_args,
        )
    elif media_type == "audio":
        duration, artist, title, _, _ = await get_media_info(media_path)
        await bot.send_audio(
            chat_id=target_chat_id,
            audio=media_path,
            duration=duration,
            performer=artist,
            title=title,
            caption=caption or "",
            progress=progress_for_pyrogram, # UPDATED
            progress_args=progress_args,
        )
    elif media_type == "document":
        await bot.send_document(
            chat_id=target_chat_id,
            document=media_path,
            caption=caption or "",
            progress=progress_for_pyrogram, # UPDATED
            progress_args=progress_args,
        )


async def download_single_media(msg, progress_message, start_time):
    try:
        media_path = await msg.download(
            progress=progress_for_pyrogram, # UPDATED
            progress_args=progressArgs(
                "📥 Downloading Progress", progress_message, start_time
            ),
        )

        parsed_caption = await get_parsed_msg(
            msg.caption or "", msg.caption_entities
        )

        if msg.photo:
            return ("success", media_path, InputMediaPhoto(media=media_path, caption=parsed_caption))
        elif msg.video:
            return ("success", media_path, InputMediaVideo(media=media_path, caption=parsed_caption))
        elif msg.document:
            return ("success", media_path, InputMediaDocument(media=media_path, caption=parsed_caption))
        elif msg.audio:
            return ("success", media_path, InputMediaAudio(media=media_path, caption=parsed_caption))

    except Exception as e:
        LOGGER(__name__).info(f"Error downloading media: {e}")
        return ("error", None, None)

    return ("skip", None, None)


async def processMediaGroup(chat_message, bot, message, destination_chat_id=None):
    media_group_messages = await chat_message.get_media_group()
    valid_media = []
    temp_paths = []
    invalid_paths = []
    
    # Target chat determination
    target_chat_id = destination_chat_id if destination_chat_id else message.chat.id

    start_time = time.time() # Fixed: use time.time() instead of time() if import is not 'from time import time'
    # Actually utils.py does 'import time' and 'from time import time'.
    # I'll stick to 'time.time()' or just 'time()' if imported. 
    # In this file imports are: 'import time' AND 'from time import time'. 
    # Let's use `time.time()` to be safe as `time` module is imported.
    start_time = time.time()
    
    progress_message = await message.reply("📥 Downloading media group...")
    LOGGER(__name__).info(
        f"Downloading media group with {len(media_group_messages)} items..."
    )

    download_tasks = []
    for msg in media_group_messages:
        if msg.photo or msg.video or msg.document or msg.audio:
            download_tasks.append(download_single_media(msg, progress_message, start_time))

    results = await asyncio.gather(*download_tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            LOGGER(__name__).error(f"Download task failed: {result}")
            continue

        status, media_path, media_obj = result
        if status == "success" and media_path and media_obj:
            temp_paths.append(media_path)
            valid_media.append(media_obj)
        elif status == "error" and media_path:
            invalid_paths.append(media_path)

    LOGGER(__name__).info(f"Valid media count: {len(valid_media)}")

    if valid_media:
        try:
            await bot.send_media_group(chat_id=target_chat_id, media=valid_media)
            await progress_message.delete()
        except Exception:
            await message.reply(
                "**❌ Failed to send media group, trying individual uploads**"
            )
            for media in valid_media:
                try:
                    # Fallback individual sends must also respect target_chat_id
                    if isinstance(media, InputMediaPhoto):
                        await bot.send_photo(
                            chat_id=target_chat_id,
                            photo=media.media,
                            caption=media.caption,
                        )
                    elif isinstance(media, InputMediaVideo):
                        await bot.send_video(
                            chat_id=target_chat_id,
                            video=media.media,
                            caption=media.caption,
                        )
                    elif isinstance(media, InputMediaDocument):
                        await bot.send_document(
                            chat_id=target_chat_id,
                            document=media.media,
                            caption=media.caption,
                        )
                    elif isinstance(media, InputMediaAudio):
                        await bot.send_audio(
                            chat_id=target_chat_id,
                            audio=media.media,
                            caption=media.caption,
                        )
                    elif isinstance(media, Voice):
                        await bot.send_voice(
                            chat_id=target_chat_id,
                            voice=media.media,
                            caption=media.caption,
                        )
                except Exception as individual_e:
                    await message.reply(
                        f"Failed to upload individual media: {individual_e}"
                    )

            await progress_message.delete()

        for path in temp_paths + invalid_paths:
            cleanup_download(path)
        return True

    await progress_message.delete()
    await message.reply("❌ No valid media found in the media group.")
    for path in invalid_paths:
        cleanup_download(path)
    return False
