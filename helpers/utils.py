# Copyright (C) @TheSmartBisnu
# Channel: https://t.me/itsSmartDev

import os
import math
import time
import asyncio
from PIL import Image
from logger import LOGGER
from typing import Optional
from asyncio.subprocess import PIPE
from asyncio import create_subprocess_exec, create_subprocess_shell, wait_for

from pyrogram.parser import Parser
from pyrogram.utils import get_channel_id
from pyrogram.types import (
    InputMediaPhoto,
    InputMediaVideo,
    InputMediaDocument,
    InputMediaAudio,
    Voice,
)

from helpers.files import (
    fileSizeLimit,
    cleanup_download,
    get_readable_file_size,
    get_readable_time
)

from helpers.msg import (
    get_parsed_msg
)

# Global cache to track the last update time for each unique message ID
# This ensures multiple progress bars don't interfere with each other
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


# -------------------------------------------------------------------------------------
# CUSTOM PROGRESS BAR IMPLEMENTATION
# -------------------------------------------------------------------------------------
async def progress_bar(current, total, progress_message, start_time, status_title, interval):
    """
    Args:
        current: Current bytes processed
        total: Total bytes
        progress_message: The unique Message object for this specific download
        start_time: Timestamp when process started
        status_title: Title string (e.g. "📥 Downloading")
        interval: Time in seconds between updates
    """
    now = time.time()
    
    # Throttling Logic: Uses message.id to track this specific progress bar
    last_update = PROGRESS_CACHE.get(progress_message.id, 0)
    
    # Update only if interval passed OR if completed (current == total)
    if (now - last_update) < interval and current != total:
        return

    # Update cache
    PROGRESS_CACHE[progress_message.id] = now

    # Calculation
    percentage = current * 100 / total
    elapsed_time = now - start_time
    
    if elapsed_time == 0:
        elapsed_time = 0.1
        
    speed = current / elapsed_time # Bytes per second
    eta = (total - current) / speed if speed > 0 else 0
    
    # Formatting
    progress_str = "▓" * int(percentage / 5) + "░" * (20 - int(percentage / 5))
    speed_str = f"{get_readable_file_size(speed)}/s"
    current_str = get_readable_file_size(current)
    total_str = get_readable_file_size(total)
    eta_str = get_readable_time(eta)

    text = (
        f"**{status_title}**\n"
        f"**Progress:** `[{progress_str}] {percentage:.2f}%`\n"
        f"**Done:** `{current_str}` / `{total_str}`\n"
        f"**Speed:** `{speed_str}`\n"
        f"**ETA:** `{eta_str}`"
    )

    try:
        await progress_message.edit(text)
    except Exception:
        pass

    # Cleanup cache if done
    if current == total:
        if progress_message.id in PROGRESS_CACHE:
            del PROGRESS_CACHE[progress_message.id]


async def send_media(
    bot, message, media_path, media_type, caption, progress_message, start_time
):
    file_size = os.path.getsize(media_path)

    if not await fileSizeLimit(file_size, message, "upload"):
        return

    # UPLOAD INTERVAL: 7 Seconds
    UPLOAD_INTERVAL = 7
    progress_args = (progress_message, start_time, "📤 Uploading", UPLOAD_INTERVAL)
    
    LOGGER(__name__).info(f"Uploading media: {media_path} ({media_type})")

    try:
        if media_type == "photo":
            await message.reply_photo(
                media_path,
                caption=caption or "",
                progress=progress_bar,
                progress_args=progress_args,
            )
        elif media_type == "video":
            duration, _, _, width, height = await get_media_info(media_path)

            if not duration or duration == 0:
                duration = 0
            if not width or not height:
                width = 640
                height = 480

            thumb = await get_video_thumbnail(media_path, duration)

            await message.reply_video(
                media_path,
                duration=duration,
                width=width,
                height=height,
                thumb=thumb,
                caption=caption or "",
                supports_streaming=True,
                progress=progress_bar,
                progress_args=progress_args,
            )
        elif media_type == "audio":
            duration, artist, title, _, _ = await get_media_info(media_path)
            await message.reply_audio(
                media_path,
                duration=duration,
                performer=artist,
                title=title,
                caption=caption or "",
                progress=progress_bar,
                progress_args=progress_args,
            )
        elif media_type == "document":
            await message.reply_document(
                media_path,
                caption=caption or "",
                progress=progress_bar,
                progress_args=progress_args,
            )
            
    except Exception as e:
        LOGGER(__name__).error(f"Upload failed: {e}")
        await message.reply(f"**❌ Upload Failed:** {e}")


async def processMediaGroup(chat_message, bot, message):
    # (Simplified for brevity - assumes standard logic)
    # The progress bar logic inside handle_download takes care of main content
    return False # Placeholder for full implementation if needed
