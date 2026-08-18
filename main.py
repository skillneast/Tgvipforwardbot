import asyncio
import os
import random
import re
import sqlite3
import time
from typing import Optional, Tuple

import nest_asyncio
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pyrogram import Client, filters
from pyrogram.errors import (
    ChatAdminRequired,
    ChatWriteForbidden,
    FloodWait,
    MessageNotModified,
    PeerIdInvalid,
)
from pyrogram.file_id import FileId
from pyrogram.raw import functions, types
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# Asyncio Event Loop Fix for Jupyter / Colab / Async runners
nest_asyncio.apply()

# ==================== CONFIGURATION ====================
API_ID = int(os.environ.get("API_ID", 33720317))
API_HASH = os.environ.get("API_HASH", "145db99951f44490f134ac7446126630")
SESSION_STRING = os.environ.get(
    "SESSION_STRING",
    "BQFSZo0AI9u3vxq2VRuJpcnzjr1DqBN6ADUOx8YiP14CO7Lmpqwx3fLFr-PKI0Dsw-sfaImZFWYPt9icc0U7GkLakeV9qCR2pXHUpSN6B6yDYg9EWYmCCCW8H6eDWwjwJLkDxHcDuvP7zFq5Idb1FzovTuow0SPL9engHMjM2FJi3i_wTYVwwknN9vvgZ2YdnzERY_MYXNvo7UZnD_1B8jXEx1U19PRYCHd9RWjpWltMX5fn3_5DgE72DOiPhx-qW4TfIrFu2GuozNyM0JVQdS7vQCR6mYusa7gJIjZt9n6e3CjGdq5plkgB098r0iNINQzIlPCp8aM0ULmxqV2E89hxEAyGqAAAAAIWPSnIAA"
)
DELAY_SECONDS = int(os.environ.get("DELAY_SECONDS", 3))
PORT = int(os.environ.get("PORT", 8080))
DB_NAME = "final_complete_dashboard_userbot.db"
CUSTOM_THUMB_PATH = "custom_thumb.jpg"

# ==================== DATABASE SETUP ====================
def get_db():
    return sqlite3.connect(DB_NAME, timeout=15)

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS task_state (
            id INTEGER PRIMARY KEY,
            source_chat TEXT,
            dest_chat TEXT,
            start_id INTEGER,
            current_id INTEGER,
            last_id INTEGER,
            copied_count INTEGER,
            videos_count INTEGER,
            thumbs_applied_count INTEGER,
            texts_count INTEGER,
            branded_count INTEGER,
            status TEXT
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('brand_name', '@skillneast1')")
    conn.commit()
    conn.close()

def get_config(key: str, default: Optional[str] = None) -> Optional[str]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default

def set_config(key: str, value: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def save_progress(
    source_chat: str,
    dest_chat: str,
    start_id: int,
    current_id: int,
    last_id: int,
    copied_count: int,
    videos_count: int,
    thumbs_applied_count: int,
    texts_count: int,
    branded_count: int,
    status: str,
):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO task_state (
            id, source_chat, dest_chat, start_id, current_id, last_id,
            copied_count, videos_count, thumbs_applied_count, texts_count, branded_count, status
        )
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(source_chat), str(dest_chat), start_id, current_id, last_id,
        copied_count, videos_count, thumbs_applied_count, texts_count, branded_count, status
    ))
    conn.commit()
    conn.close()

def delete_task_progress():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM task_state WHERE id = 1")
    conn.commit()
    conn.close()

def get_progress():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT source_chat, dest_chat, start_id, current_id, last_id,
               copied_count, videos_count, thumbs_applied_count, texts_count, branded_count, status
        FROM task_state WHERE id = 1
    """)
    row = cursor.fetchone()
    conn.close()
    return row

init_db()

# ==================== PYROGRAM CLIENT & GLOBAL STATE ====================
app = Client(
    "final_complete_userbot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

task_running = False
is_paused = False
task_cancelled = False
task_start_time = 0.0
active_dashboard_msg: Optional[Message] = None
cached_input_photo: Optional[types.InputPhoto] = None

# ==================== UI & CALCULATION HELPERS ====================
def format_time(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours:02d}h {minutes:02d}m {secs:02d}s"
    return f"{minutes:02d}m {secs:02d}s"

def generate_progress_bar(percentage: float, length: int = 12) -> str:
    filled = int(round(length * (percentage / 100)))
    filled = max(0, min(length, filled))
    empty = length - filled
    return "█" * filled + "░" * empty

def process_caption(caption_text: str, is_pure_text: bool = False) -> Tuple[str, bool]:
    """
    1. If @username exists -> 100% replace with current brand watermark (branded = True).
    2. If short title/header text -> keep clean without watermark (branded = False).
    3. If clean caption or empty media -> True random 40% chance of adding watermark.
    """
    brand = get_config("brand_name", "@skillneast1")

    # Rule 1: Always replace external usernames
    if caption_text:
        usernames = re.findall(r"@[a-zA-Z0-9_]+", caption_text)
        if usernames:
            return re.sub(r"@[a-zA-Z0-9_]+", brand, caption_text), True

    # Rule 2: Short text / title protection
    if is_pure_text and caption_text:
        clean_txt = caption_text.strip()
        if len(clean_txt) <= 30 or clean_txt.lower() in ["welcome", "complete", "notes", "index", "module"]:
            return caption_text, False

    # Rule 3: True Random 40% Roll
    should_brand = random.random() < 0.40

    if should_brand:
        if caption_text:
            return f"{caption_text}\n\nProvided by {brand}", True
        else:
            return f"Provided by {brand}", True
    else:
        return caption_text if caption_text else "", False

def render_dashboard(
    source_chat: str,
    dest_chat: str,
    brand: str,
    start_id: int,
    current_id: int,
    last_id: int,
    copied_count: int,
    videos_count: int,
    thumbs_applied_count: int,
    texts_count: int,
    branded_count: int,
    status_label: str,
    start_time: float,
) -> Tuple[str, InlineKeyboardMarkup]:
    total_msgs = max(1, (last_id - start_id) + 1)
    processed_count = max(0, min(total_msgs, (current_id - start_id) + 1))
    remaining_msgs = max(0, last_id - current_id)
    percentage = round((processed_count / total_msgs) * 100, 1)
    bar = generate_progress_bar(percentage)

    # Time & Speed calculations
    elapsed_sec = max(0.1, time.time() - start_time) if start_time > 0 else 0.1
    elapsed_str = format_time(elapsed_sec)

    speed_per_sec = processed_count / elapsed_sec if elapsed_sec > 0 else 0
    speed_per_min = round(speed_per_sec * 60, 1)

    if speed_per_sec > 0:
        eta_sec = remaining_msgs / speed_per_sec
    else:
        eta_sec = remaining_msgs * DELAY_SECONDS
    eta_str = format_time(eta_sec) if remaining_msgs > 0 else "00m 00s"

    card = (
        "<b>🚀 REAL-TIME LIVE DASHBOARD</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Source Chat:</b> <code>{source_chat}</code>\n"
        f"🎯 <b>Target Chat:</b> <code>{dest_chat}</code>\n"
        f"🎨 <b>Brand Tag:</b> <code>{brand}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>PROGRESS:</b> <code>[{bar}]</code> <b>{percentage}%</b>\n\n"
        f"🔢 <b>Current Message ID:</b> <code>{current_id}</code> / <code>{last_id}</code>\n"
        f"📦 <b>Total in Range:</b> <code>{total_msgs}</code> msgs\n"
        f"✅ <b>Processed / Copied:</b> <code>{copied_count}</code>\n"
        f"⏳ <b>Remaining to Copy:</b> <code>{remaining_msgs}</code> msgs\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>📂 MEDIA BREAKDOWN:</b>\n"
        f"• 🎥 <b>Total Videos:</b> <code>{videos_count}</code>\n"
        f"• 🖼️ <b>Thumbnails Applied:</b> <code>{thumbs_applied_count}</code>\n"
        f"• 📝 <b>Text / Other Files:</b> <code>{texts_count}</code>\n"
        f"• 🏷️ <b>Branded Captions:</b> <code>{branded_count}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱️ <b>Elapsed Time:</b> <code>{elapsed_str}</code>\n"
        f"⌛ <b>Estimated Time (ETA):</b> <code>{eta_str}</code>\n"
        f"⚡ <b>Transfer Speed:</b> <code>{speed_per_min} msgs/min</code>\n"
        f"📶 <b>Current State:</b> <code>{status_label}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏸️ Pause", callback_data="btn_pause"),
            InlineKeyboardButton("▶️ Resume", callback_data="btn_resume")
        ],
        [
            InlineKeyboardButton("🔄 Live Refresh (/ld)", callback_data="btn_status"),
            InlineKeyboardButton("🛑 Cancel Task", callback_data="btn_stop")
        ]
    ])

    return card, keyboard

def parse_telegram_link(link: str) -> Tuple[Optional[int | str], Optional[int], Optional[int]]:
    link = link.strip()
    p_range = re.search(r"t\.me/c/(\d+)/(\d+)-(\d+)", link)
    if p_range:
        return int("-100" + p_range.group(1)), int(p_range.group(2)), int(p_range.group(3))

    p_single = re.search(r"t\.me/c/(\d+)/(\d+)", link)
    if p_single:
        start = int(p_single.group(2))
        return int("-100" + p_single.group(1)), start, start + 250000

    pub_range = re.search(r"t\.me/([^/]+)/(\d+)-(\d+)", link)
    if pub_range:
        return pub_range.group(1), int(pub_range.group(2)), int(pub_range.group(3))

    pub_single = re.search(r"t\.me/([^/]+)/(\d+)", link)
    if pub_single:
        start = int(pub_single.group(2))
        return pub_single.group(1), start, start + 250000

    return None, None, None

async def sync_dialogs(client: Client) -> bool:
    try:
        async for _ in client.get_dialogs():
            pass
        return True
    except Exception:
        return False

async def get_or_upload_cover_photo(client: Client) -> Optional[types.InputPhoto]:
    """Uploads the local custom thumbnail photo once to Telegram MTProto and caches the InputPhoto handle."""
    global cached_input_photo
    if cached_input_photo:
        return cached_input_photo

    if not os.path.exists(CUSTOM_THUMB_PATH):
        return None

    try:
        input_file = await client.save_file(CUSTOM_THUMB_PATH)
        res = await client.invoke(
            functions.messages.UploadMedia(
                peer=types.InputPeerSelf(),
                media=types.InputMediaUploadedPhoto(file=input_file),
            )
        )
        if hasattr(res, "photo") and isinstance(res.photo, types.Photo):
            cached_input_photo = types.InputPhoto(
                id=res.photo.id,
                access_hash=res.photo.access_hash,
                file_reference=res.photo.file_reference,
            )
            return cached_input_photo
    except Exception as e:
        print(f"⚠️ Error uploading cover photo: {e}")
    return None

async def send_video_with_instant_cover(
    client: Client,
    dest_peer,
    msg: Message,
    caption: str,
    cover_photo: types.InputPhoto,
) -> bool:
    """Uses MTProto video_cover to instantly attach custom thumbnail to cloud video without downloading."""
    try:
        decoded = FileId.decode(msg.video.file_id)
        input_doc = types.InputDocument(
            id=decoded.media_id,
            access_hash=decoded.access_hash,
            file_reference=decoded.file_reference,
        )

        input_media = types.InputMediaDocument(
            id=input_doc,
            video_cover=cover_photo,
            spoiler=bool(getattr(msg, "has_media_spoiler", False)),
        )

        await client.invoke(
            functions.messages.SendMedia(
                peer=dest_peer,
                media=input_media,
                message=caption,
                random_id=client.rnd_id(),
            )
        )
        return True
    except Exception as e:
        print(f"⚠️ Video cover MTProto fallback triggered: {e}")
        return False

# ==================== FASTAPI WEB SERVER (24/7 KEEP-ALIVE) ====================
web_app = FastAPI()

@web_app.get("/")
@web_app.get("/health")
async def health_check():
    return JSONResponse(
        status_code=200,
        content={
            "status": "online",
            "task_running": task_running,
            "is_paused": is_paused,
            "service": "Advanced Live Dashboard Userbot"
        }
    )

async def start_web_server():
    config = uvicorn.Config(app=web_app, host="0.0.0.0", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()

# ==================== COMMAND HANDLERS ====================
ALLOWED_FILTER = (filters.me | filters.private)

@app.on_message(ALLOWED_FILTER & filters.command(["start", "help"], prefixes=["/", "."]))
async def start_command(client: Client, message: Message):
    target = get_config("target_chat", "❌ Not Configured")
    brand = get_config("brand_name", "@skillneast1")

    welcome_text = (
        "<b>🤖 Personal Content Forwarder Userbot</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Target Channel:</b> <code>{target}</code>\n"
        f"🎨 <b>Active Brand:</b> <code>{brand}</code>\n\n"
        "<b>📖 Available Commands:</b>\n"
        "• <code>/copy &lt;link&gt;</code> — Start copy directly with Live Dashboard\n"
        "• <code>/ld</code> — View instant live progress dashboard\n"
        "• <code>/cancel</code> — Stop current task & delete checkpoint\n"
        "• <code>/pause</code> — Temporarily pause the running task\n"
        "• <code>/resume</code> — Resume only if task was paused\n"
        "• <code>/settarget &lt;id&gt;</code> — Set destination channel ID\n"
        "• <code>/setbrand &lt;name&gt;</code> — Change caption brand watermark\n"
        "• <code>/getbrand</code> — Check current brand name\n"
        "• <code>/setthumb</code> — Reply to a photo to set custom thumbnail\n"
        "• <code>/sync</code> — Sync channel dialogs & access hashes\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Live Dashboard (/ld)", callback_data="btn_status"),
            InlineKeyboardButton("🎨 Brand Info", callback_data="btn_brand")
        ],
        [
            InlineKeyboardButton("⏸️ Pause", callback_data="btn_pause"),
            InlineKeyboardButton("▶️ Resume", callback_data="btn_resume")
        ],
        [
            InlineKeyboardButton("🛑 Cancel Task", callback_data="btn_stop")
        ]
    ])

    await message.reply_text(welcome_text, reply_markup=keyboard, disable_web_page_preview=True)

@app.on_message(ALLOWED_FILTER & filters.command(["setthumb"], prefixes=["/", "."]))
async def set_thumb(client: Client, message: Message):
    global cached_input_photo
    if message.reply_to_message and message.reply_to_message.photo:
        status_msg = await message.reply_text("📥 <i>Downloading and caching custom thumbnail...</i>")
        await client.download_media(message.reply_to_message.photo, file_name=CUSTOM_THUMB_PATH)
        cached_input_photo = None
        await get_or_upload_cover_photo(client)
        await status_msg.edit_text(
            "<b>✅ Custom Thumbnail Saved & Cached!</b>\n\n"
            "✨ <b>Combined Rule Active:</b>\n"
            "• Videos with original thumbnails $\\rightarrow$ 100% custom thumbnail.\n"
            "• Clean videos in the 40% brand pool $\\rightarrow$ Custom thumbnail attached.\n"
            "• Clean videos in the 60% pool $\\rightarrow$ Left original without thumbnail."
        )
    else:
        await message.reply_text("❌ <b>Error:</b> Please reply to an image with <code>/setthumb</code>.")

@app.on_message(ALLOWED_FILTER & filters.command(["cancel", "stop"], prefixes=["/", "."]))
async def cancel_command(client: Client, message: Message):
    global task_running, is_paused, task_cancelled
    if task_running or is_paused:
        task_cancelled = True
        task_running = False
        is_paused = False
        delete_task_progress()
        await message.reply_text(
            "<b>🛑 Task Cancelled & Deleted!</b>\n\n"
            "Task ko permanently rok diya gaya hai aur data delete kar diya gaya hai. "
            "Ise <code>/resume</code> se wapas chalu nahi kiya ja sakta."
        )
    else:
        delete_task_progress()
        await message.reply_text("ℹ️ <b>No active or paused task to cancel. Database cleared.</b>")

@app.on_message(ALLOWED_FILTER & filters.command(["ld", "status"], prefixes=["/", "."]))
async def ld_command(client: Client, message: Message):
    await send_status_view(message)

async def send_status_view(target_ctx: Message | CallbackQuery):
    global active_dashboard_msg
    saved = get_progress()
    target_config = get_config("target_chat", "❌ Not Configured")
    brand = get_config("brand_name", "@skillneast1")

    if task_running or (saved and saved[11] in ["RUNNING", "PAUSED"]):
        (
            source_chat, dest_chat, start_id, current_id, last_id,
            copied_count, videos_count, thumbs_applied_count, texts_count, branded_count, status
        ) = saved
        status_label = "PAUSED ⏸️" if is_paused else "RUNNING 🟢"
        card_text, keyboard = render_dashboard(
            source_chat=source_chat,
            dest_chat=dest_chat,
            brand=brand,
            start_id=start_id,
            current_id=current_id,
            last_id=last_id,
            copied_count=copied_count,
            videos_count=videos_count,
            thumbs_applied_count=thumbs_applied_count,
            texts_count=texts_count,
            branded_count=branded_count,
            status_label=status_label,
            start_time=task_start_time,
        )
    else:
        card_text = (
            "<b>📊 Live Task Dashboard</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "ℹ️ <i>No task is actively running.</i>\n\n"
            f"🎯 <b>Configured Target:</b> <code>{target_config}</code>\n"
            f"🎨 <b>Active Brand:</b> <code>{brand}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh", callback_data="btn_status")]
        ])

    if isinstance(target_ctx, Message):
        sent_msg = await target_ctx.reply_text(card_text, reply_markup=keyboard, disable_web_page_preview=True)
        if task_running:
            active_dashboard_msg = sent_msg
    elif isinstance(target_ctx, CallbackQuery):
        try:
            await target_ctx.message.edit_text(card_text, reply_markup=keyboard, disable_web_page_preview=True)
        except (MessageNotModified, Exception):
            pass
        await target_ctx.answer("Dashboard Refreshed")

@app.on_message(ALLOWED_FILTER & filters.command(["pause"], prefixes=["/", "."]))
async def pause_task(client: Client, message: Message):
    global is_paused
    if task_running and not is_paused:
        is_paused = True
        await message.reply_text("⏸️ <b>Task Paused Successfully.</b>\nUse <code>/resume</code> jab aap wapas chalu karna chahein.")
    else:
        await message.reply_text("❌ <b>No active copy task is currently running to pause.</b>")

@app.on_message(ALLOWED_FILTER & filters.command(["resume"], prefixes=["/", "."]))
async def resume_task(client: Client, message: Message):
    global is_paused, task_running, task_start_time, task_cancelled
    if task_running and is_paused:
        is_paused = False
        await message.reply_text("▶️ <b>Task Resumed. Continuing message processing...</b>")
    elif not task_running:
        saved = get_progress()
        if saved and saved[11] == "PAUSED":
            (
                source_chat, dest_chat, start_id, current_id, last_id,
                copied_count, videos_count, thumbs_applied_count, texts_count, branded_count, _
            ) = saved
            is_paused = False
            task_cancelled = False
            task_start_time = time.time()
            asyncio.create_task(
                run_copy_process(
                    client, message, source_chat, dest_chat, start_id, current_id, last_id,
                    copied_count, videos_count, thumbs_applied_count, texts_count, branded_count
                )
            )
            await message.reply_text(f"▶️ <b>Resuming paused task from Message ID:</b> <code>{current_id}</code>")
        else:
            await message.reply_text(
                "❌ <b>No paused task found to resume!</b>\n\n"
                "Resume sirf tab kaam karta hai jab aapne <code>/pause</code> kiya ho. "
                "Cancel kiya hua task resume nahi hota."
            )

@app.on_message(ALLOWED_FILTER & filters.command(["setbrand"], prefixes=["/", "."]))
async def set_brand_command(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        current = get_config("brand_name", "@skillneast1")
        await message.reply_text(f"ℹ️ <b>Current Brand:</b> <code>{current}</code>\n\nUsage: <code>/setbrand &lt;new_brand_name&gt;</code>")
        return

    old_brand = get_config("brand_name", "@skillneast1")
    new_brand = args[1].strip()
    set_config("brand_name", new_brand)

    await message.reply_text(
        f"<b>✅ Brand Watermark Updated!</b>\n\n"
        f"<b>Old:</b> <code>{old_brand}</code>\n"
        f"<b>New:</b> <code>{new_brand}</code>"
    )

@app.on_message(ALLOWED_FILTER & filters.command(["getbrand", "brand"], prefixes=["/", "."]))
async def get_brand_command(client: Client, message: Message):
    brand = get_config("brand_name", "@skillneast1")
    await message.reply_text(f"🎨 <b>Current Brand:</b> <code>{brand}</code>")

@app.on_message(ALLOWED_FILTER & filters.command(["sync"], prefixes=["/", "."]))
async def sync_command(client: Client, message: Message):
    status_msg = await message.reply_text("🔄 <i>Syncing joined channels and resolving peer access hashes...</i>")
    success = await sync_dialogs(client)
    if success:
        await status_msg.edit_text("<b>✅ All joined channels synced into local cache!</b>")
    else:
        await status_msg.edit_text("<b>⚠️ Sync completed with non-fatal warnings.</b>")

@app.on_message(ALLOWED_FILTER & filters.command(["settarget"], prefixes=["/", "."]))
async def set_target_cmd(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.reply_text("❌ <b>Error:</b> Provide Target Channel ID.\nExample: <code>/settarget -1004415448802</code>")
        return

    target_chat = args[1].strip()
    set_config("target_chat", target_chat)
    await message.reply_text(
        f"<b>✅ Target Channel Configured!</b>\n\n"
        f"🎯 <b>Channel ID:</b> <code>{target_chat}</code>\n"
        f"<i>Make sure your account is an Admin with post permissions.</i>"
    )

@app.on_callback_query()
async def handle_callbacks(client: Client, callback: CallbackQuery):
    global is_paused, task_running, task_cancelled, task_start_time
    data = callback.data

    if data == "btn_status":
        await send_status_view(callback)

    elif data == "btn_pause":
        if task_running and not is_paused:
            is_paused = True
            await callback.answer("Task Paused ⏸️", show_alert=False)
            await send_status_view(callback)
        else:
            await callback.answer("No running task to pause.", show_alert=True)

    elif data == "btn_resume":
        if task_running and is_paused:
            is_paused = False
            await callback.answer("Task Resumed ▶️", show_alert=False)
            await send_status_view(callback)
        elif not task_running:
            saved = get_progress()
            if saved and saved[11] == "PAUSED":
                (
                    source_chat, dest_chat, start_id, current_id, last_id,
                    copied_count, videos_count, thumbs_applied_count, texts_count, branded_count, _
                ) = saved
                is_paused = False
                task_cancelled = False
                task_start_time = time.time()
                asyncio.create_task(
                    run_copy_process(
                        client, callback.message, source_chat, dest_chat, start_id, current_id, last_id,
                        copied_count, videos_count, thumbs_applied_count, texts_count, branded_count
                    )
                )
                await callback.answer("Resumed from checkpoint!", show_alert=False)
                await send_status_view(callback)
            else:
                await callback.answer("No paused task found to resume.", show_alert=True)
        else:
            await callback.answer("Task already running.", show_alert=True)

    elif data == "btn_stop":
        if task_running or is_paused:
            task_cancelled = True
            task_running = False
            is_paused = False
            delete_task_progress()
            await callback.answer("Task Cancelled & Deleted 🛑", show_alert=True)
            await send_status_view(callback)
        else:
            await callback.answer("No active task to cancel.", show_alert=True)

    elif data == "btn_brand":
        brand = get_config("brand_name", "@skillneast1")
        await callback.answer(f"Active Brand: {brand}", show_alert=True)

# ==================== MAIN COPY COMMAND ====================
@app.on_message(ALLOWED_FILTER & filters.command(["copy"], prefixes=["/", "."]))
async def start_copy_command(client: Client, message: Message):
    global task_running, is_paused, task_cancelled, task_start_time

    if task_running:
        await message.reply_text("⚠️ <b>A task is already running!</b> Use <code>/ld</code> to check or <code>/cancel</code> to stop.")
        return

    dest_chat = get_config("target_chat")
    if not dest_chat:
        await message.reply_text("❌ <b>Target Channel Not Configured!</b>\nSet it first: <code>/settarget &lt;channel_id&gt;</code>")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.reply_text(
            "❌ <b>Usage:</b> <code>/copy &lt;telegram_link&gt;</code>\n"
            "Example: <code>/copy https://t.me/c/4429284952/78118-78139</code>"
        )
        return

    source_chat, start_msg_id, end_msg_id = parse_telegram_link(args[1])
    if not source_chat or not start_msg_id or not end_msg_id:
        await message.reply_text("❌ <b>Invalid Link Format!</b> Use format: <code>https://t.me/c/4429284952/78118-78139</code>")
        return

    is_paused = False
    task_cancelled = False
    task_start_time = time.time()
    save_progress(str(source_chat), str(dest_chat), start_msg_id, start_msg_id, end_msg_id, 0, 0, 0, 0, 0, "RUNNING")

    # Start copy task with live dashboard
    asyncio.create_task(
        run_copy_process(
            client, message, source_chat, dest_chat, start_msg_id, start_msg_id, end_msg_id,
            0, 0, 0, 0, 0
        )
    )

# ==================== CORE WORKER WITH ADVANCED DASHBOARD ====================
async def run_copy_process(
    client: Client,
    notify_message: Message,
    source_chat: int | str,
    dest_chat: int | str,
    start_id: int,
    current_start: int,
    last_id: int,
    initial_copied_count: int,
    initial_videos_count: int,
    initial_thumbs_applied_count: int,
    initial_texts_count: int,
    initial_branded_count: int,
):
    global task_running, is_paused, task_cancelled, task_start_time, active_dashboard_msg
    task_running = True
    is_paused = False
    task_cancelled = False

    # Resolve Chat Objects
    try:
        source_chat_obj = await client.get_chat(source_chat)
        dest_chat_obj = await client.get_chat(dest_chat)
    except PeerIdInvalid:
        await sync_dialogs(client)
        try:
            source_chat_obj = await client.get_chat(source_chat)
            dest_chat_obj = await client.get_chat(dest_chat)
        except Exception as e:
            await notify_message.reply_text(f"❌ <b>Peer Error:</b> <code>{e}</code>\nEnsure account is a member in source and admin in target.")
            task_running = False
            return
    except Exception as e:
        await notify_message.reply_text(f"❌ <b>Chat Access Error:</b> <code>{e}</code>")
        task_running = False
        return

    # Pre-fetch Destination Raw Peer and Cover Photo
    dest_peer = await client.resolve_peer(dest_chat_obj.id)
    cover_photo = await get_or_upload_cover_photo(client)

    brand = get_config("brand_name", "@skillneast1")
    current_id = current_start
    copied_count = initial_copied_count
    videos_count = initial_videos_count
    thumbs_applied_count = initial_thumbs_applied_count
    texts_count = initial_texts_count
    branded_count = initial_branded_count

    # 1. SEND LIVE DASHBOARD DIRECTLY ON /copy
    initial_card, initial_keyboard = render_dashboard(
        source_chat=str(source_chat),
        dest_chat=str(dest_chat),
        brand=brand,
        start_id=start_id,
        current_id=current_id,
        last_id=last_id,
        copied_count=copied_count,
        videos_count=videos_count,
        thumbs_applied_count=thumbs_applied_count,
        texts_count=texts_count,
        branded_count=branded_count,
        status_label="RUNNING 🟢",
        start_time=task_start_time,
    )
    dashboard_msg: Message = await notify_message.reply_text(
        initial_card,
        reply_markup=initial_keyboard,
        disable_web_page_preview=True,
    )
    active_dashboard_msg = dashboard_msg

    last_dashboard_edit_time = time.time()

    while current_id <= last_id:
        if task_cancelled:
            delete_task_progress()
            task_running = False
            return

        while is_paused:
            save_progress(
                str(source_chat), str(dest_chat), start_id, current_id, last_id,
                copied_count, videos_count, thumbs_applied_count, texts_count, branded_count, "PAUSED"
            )
            await asyncio.sleep(2)
            if task_cancelled:
                delete_task_progress()
                task_running = False
                return

        try:
            msg: Message = await client.get_messages(source_chat_obj.id, current_id)
            if msg and not msg.empty and not msg.service:
                raw_caption = msg.caption or msg.text or ""
                is_pure_text = bool(msg.text and not msg.media)
                final_caption, was_branded = process_caption(raw_caption, is_pure_text=is_pure_text)

                if was_branded:
                    branded_count += 1

                try:
                    # ==================== VIDEO HANDLING WITH INSTANT COVER ====================
                    if msg.video:
                        videos_count += 1
                        has_original_thumb = bool(msg.video.thumbs or msg.video.thumbnail)
                        
                        # COMBINED RULE: Apply thumbnail if original had thumb OR was branded in 40% pool
                        should_apply_thumb = (has_original_thumb or was_branded) and (cover_photo is not None)

                        if should_apply_thumb:
                            success = await send_video_with_instant_cover(
                                client=client,
                                dest_peer=dest_peer,
                                msg=msg,
                                caption=final_caption,
                                cover_photo=cover_photo,
                            )
                            if success:
                                thumbs_applied_count += 1
                            else:
                                await client.copy_message(
                                    chat_id=dest_chat_obj.id,
                                    from_chat_id=source_chat_obj.id,
                                    message_id=msg.id,
                                    caption=final_caption,
                                )
                        else:
                            await client.copy_message(
                                chat_id=dest_chat_obj.id,
                                from_chat_id=source_chat_obj.id,
                                message_id=msg.id,
                                caption=final_caption,
                            )
                    else:
                        texts_count += 1
                        if msg.media:
                            await client.copy_message(
                                chat_id=dest_chat_obj.id,
                                from_chat_id=source_chat_obj.id,
                                message_id=msg.id,
                                caption=final_caption,
                            )
                        elif msg.text:
                            await client.send_message(
                                chat_id=dest_chat_obj.id,
                                text=final_caption,
                            )
                    copied_count += 1
                except Exception:
                    # Fallback for protected channels
                    if msg.media:
                        file_path = await client.download_media(msg)
                        if file_path:
                            try:
                                if msg.video:
                                    videos_count += 1
                                    has_original_thumb = bool(msg.video.thumbs or msg.video.thumbnail)
                                    thumb_to_use = CUSTOM_THUMB_PATH if ((has_original_thumb or was_branded) and os.path.exists(CUSTOM_THUMB_PATH)) else None
                                    if thumb_to_use:
                                        thumbs_applied_count += 1
                                    await client.send_video(dest_chat_obj.id, video=file_path, caption=final_caption, thumb=thumb_to_use)
                                elif msg.photo:
                                    texts_count += 1
                                    await client.send_photo(dest_chat_obj.id, photo=file_path, caption=final_caption)
                                elif msg.document:
                                    texts_count += 1
                                    await client.send_document(dest_chat_obj.id, document=file_path, caption=final_caption)
                                elif msg.audio:
                                    texts_count += 1
                                    await client.send_audio(dest_chat_obj.id, audio=file_path, caption=final_caption)
                                copied_count += 1
                            finally:
                                if os.path.exists(file_path):
                                    os.remove(file_path)

                await asyncio.sleep(DELAY_SECONDS)

            current_id += 1
            save_progress(
                str(source_chat), str(dest_chat), start_id, current_id, last_id,
                copied_count, videos_count, thumbs_applied_count, texts_count, branded_count, "RUNNING"
            )

            # 2. REAL-TIME DASHBOARD AUTO-EDIT (Every 3.5 seconds)
            now = time.time()
            if (now - last_dashboard_edit_time >= 3.5) or (current_id > last_id):
                last_dashboard_edit_time = now
                updated_card, updated_keyboard = render_dashboard(
                    source_chat=str(source_chat),
                    dest_chat=str(dest_chat),
                    brand=brand,
                    start_id=start_id,
                    current_id=min(current_id, last_id),
                    last_id=last_id,
                    copied_count=copied_count,
                    videos_count=videos_count,
                    thumbs_applied_count=thumbs_applied_count,
                    texts_count=texts_count,
                    branded_count=branded_count,
                    status_label="RUNNING 🟢",
                    start_time=task_start_time,
                )
                try:
                    if active_dashboard_msg:
                        await active_dashboard_msg.edit_text(
                            updated_card,
                            reply_markup=updated_keyboard,
                            disable_web_page_preview=True
                        )
                except (MessageNotModified, FloodWait, Exception):
                    pass

        except (ChatWriteForbidden, ChatAdminRequired):
            await notify_message.reply_text(
                f"❌ <b>Permission Denied!</b> Ensure your account is an <b>ADMIN</b> in: <code>{dest_chat}</code>"
            )
            task_running = False
            delete_task_progress()
            return

        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception:
            current_id += 1
            await asyncio.sleep(1)

    task_running = False
    delete_task_progress()

    # 3. FINAL COMPLETED DASHBOARD VIEW
    completed_card, completed_keyboard = render_dashboard(
        source_chat=str(source_chat),
        dest_chat=str(dest_chat),
        brand=brand,
        start_id=start_id,
        current_id=last_id,
        last_id=last_id,
        copied_count=copied_count,
        videos_count=videos_count,
        thumbs_applied_count=thumbs_applied_count,
        texts_count=texts_count,
        branded_count=branded_count,
        status_label="COMPLETED 🎉",
        start_time=task_start_time,
    )
    try:
        if active_dashboard_msg:
            await active_dashboard_msg.edit_text(
                completed_card,
                reply_markup=completed_keyboard,
                disable_web_page_preview=True
            )
        else:
            await notify_message.reply_text(completed_card, reply_markup=completed_keyboard)
    except Exception:
        await notify_message.reply_text(completed_card, reply_markup=completed_keyboard)

# ==================== RUNNER ====================
async def main():
    await app.start()
    print("⚡ Syncing dialogs into peer cache...")
    await sync_dialogs(app)
    print("✅ Advanced Real-Time Dashboard Userbot is Online & Ready on Railway!")
    await start_web_server()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
