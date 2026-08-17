import asyncio
import os
import re
import sqlite3
from typing import Optional, Tuple

import nest_asyncio
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pyrogram import Client, filters
from pyrogram.errors import (
    ChannelPrivate,
    ChatAdminRequired,
    ChatWriteForbidden,
    FloodWait,
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

# Fix asyncio event loop for environments like Google Colab / Jupyter
nest_asyncio.apply()

# ==================== CONFIGURATION & CREDENTIALS ====================
API_ID = int(os.environ.get("API_ID", 33720317))
API_HASH = os.environ.get("API_HASH", "145db99951f44490f134ac7446126630")
SESSION_STRING = os.environ.get(
    "SESSION_STRING",
    "BQFSZo0AI9u3vxq2VRuJpcnzjr1DqBN6ADUOx8YiP14CO7Lmpqwx3fLFr-PKI0Dsw-sfaImZFWYPt9icc0U7GkLakeV9qCR2pXHUpSN6B6yDYg9EWYmCCCW8H6eDWwjwJLkDxHcDuvP7zFq5Idb1FzovTuow0SPL9engHMjM2FJi3i_wTYVwwknN9vvgZ2YdnzERY_MYXNvo7UZnD_1B8jXEx1U19PRYCHd9RWjpWltMX5fn3_5DgE72DOiPhx-qW4TfIrFu2GuozNyM0JVQdS7vQCR6mYusa7gJIjZt9n6e3CjGdq5plkgB098r0iNINQzIlPCp8aM0ULmxqV2E89hxEAyGqAAAAAIWPSnIAA"
)
DELAY_SECONDS = int(os.environ.get("DELAY_SECONDS", 3))
PORT = int(os.environ.get("PORT", 8080))

DB_NAME = "userbot_production.db"
CUSTOM_THUMB_PATH = "custom_thumb.jpg"

# ==================== DATABASE INITIALIZATION ====================
def get_db_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_NAME, timeout=15)

def init_db():
    conn = get_db_connection()
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
            status TEXT
        )
    """)
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('brand_name', '@skillneast1')")
    conn.commit()
    conn.close()

def get_config(key: str, default: Optional[str] = None) -> Optional[str]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default

def set_config(key: str, value: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()
    conn.close()

def save_progress(source_chat: str, dest_chat: str, start_id: int, current_id: int, last_id: int, copied_count: int, status: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO task_state (id, source_chat, dest_chat, start_id, current_id, last_id, copied_count, status)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?)
    """, (str(source_chat), str(dest_chat), start_id, current_id, last_id, copied_count, status))
    conn.commit()
    conn.close()

def get_progress():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT source_chat, dest_chat, start_id, current_id, last_id, copied_count, status FROM task_state WHERE id = 1")
    row = cursor.fetchone()
    conn.close()
    return row

init_db()

# ==================== PYROGRAM CLIENT & GLOBAL CACHE ====================
app = Client(
    "final_production_userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

task_running = False
is_paused = False
cached_input_photo: Optional[types.InputPhoto] = None

# ==================== HELPER & MTPROTO FUNCTIONS ====================
def generate_progress_bar(percentage: float, length: int = 10) -> str:
    filled = int(round(length * (percentage / 100)))
    filled = max(0, min(length, filled))
    empty = length - filled
    return "█" * filled + "░" * empty

def process_caption(caption_text: str) -> str:
    brand = get_config("brand_name", "@skillneast1")
    if not caption_text:
        return f"Provided by {brand}"

    usernames = re.findall(r"@[a-zA-Z0-9_]+", caption_text)
    if usernames:
        return re.sub(r"@[a-zA-Z0-9_]+", brand, caption_text)
    else:
        return f"{caption_text}\n\nProvided by {brand}"

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

# ==================== FASTAPI KEEP-ALIVE SERVER ====================
web_app = FastAPI()

@web_app.get("/")
@web_app.get("/health")
async def health_check():
    return JSONResponse(
        status_code=200,
        content={
            "status": "online",
            "bot_running": task_running,
            "is_paused": is_paused,
            "service": "Telegram Instant Userbot"
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
        "<b>🤖 Telegram Content Forwarder Userbot</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Target Channel:</b> <code>{target}</code>\n"
        f"🎨 <b>Active Brand Tag:</b> <code>{brand}</code>\n\n"
        "<b>📖 Available Commands:</b>\n"
        "• <code>/copy &lt;link&gt;</code> — Start copying message range\n"
        "• <code>/settarget &lt;id&gt;</code> — Set destination channel ID\n"
        "• <code>/setbrand &lt;name&gt;</code> — Change caption brand tag\n"
        "• <code>/getbrand</code> — Check current brand name\n"
        "• <code>/setthumb</code> — Reply to a photo to set video cover\n"
        "• <code>/status</code> — View live progress & metrics dashboard\n"
        "• <code>/pause</code> | <code>/resume</code> — Process control\n"
        "• <code>/sync</code> — Sync channel dialogs & peer cache\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Status", callback_data="btn_status"),
            InlineKeyboardButton("🎨 Brand Info", callback_data="btn_brand")
        ],
        [
            InlineKeyboardButton("⏸️ Pause", callback_data="btn_pause"),
            InlineKeyboardButton("▶️ Resume", callback_data="btn_resume")
        ]
    ])

    await message.reply_text(welcome_text, reply_markup=keyboard, disable_web_page_preview=True)

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
        f"<b>✅ Brand Name Updated Successfully!</b>\n\n"
        f"<b>Previous:</b> <code>{old_brand}</code>\n"
        f"<b>New Brand:</b> <code>{new_brand}</code>"
    )

@app.on_message(ALLOWED_FILTER & filters.command(["getbrand", "brand"], prefixes=["/", "."]))
async def get_brand_command(client: Client, message: Message):
    brand = get_config("brand_name", "@skillneast1")
    await message.reply_text(f"🎨 <b>Current Brand Name:</b> <code>{brand}</code>")

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
        await message.reply_text("❌ <b>Error:</b> Please provide Target Channel ID.\nExample: <code>/settarget -1004415448802</code>")
        return

    target_chat = args[1].strip()
    set_config("target_chat", target_chat)
    await message.reply_text(
        f"<b>✅ Target Channel Configured!</b>\n\n"
        f"🎯 <b>Channel ID:</b> <code>{target_chat}</code>\n"
        f"<i>Make sure your account is an Admin with post permissions.</i>"
    )

@app.on_message(ALLOWED_FILTER & filters.command(["setthumb"], prefixes=["/", "."]))
async def set_thumb(client: Client, message: Message):
    global cached_input_photo
    if message.reply_to_message and message.reply_to_message.photo:
        status_msg = await message.reply_text("📥 <i>Downloading and saving custom thumbnail...</i>")
        await client.download_media(message.reply_to_message.photo, file_name=CUSTOM_THUMB_PATH)
        cached_input_photo = None  # Reset cache to force upload of new thumb
        await get_or_upload_cover_photo(client)
        await status_msg.edit_text(
            "<b>✅ Custom Thumbnail Configured!</b>\n\n"
            "✨ <b>Smart Rule:</b> <i>This thumbnail will be applied instantly via video_cover ONLY to videos that already have an original thumbnail. Videos without thumbnails will be copied completely unchanged.</i>"
        )
    else:
        await message.reply_text("❌ <b>Error:</b> Please reply to an image with <code>/setthumb</code>.")

@app.on_message(ALLOWED_FILTER & filters.command(["pause"], prefixes=["/", "."]))
async def pause_task(client: Client, message: Message):
    global is_paused
    if task_running and not is_paused:
        is_paused = True
        await message.reply_text("⏸️ <b>Task Paused Successfully.</b>\nUse <code>/resume</code> to continue processing.")
    else:
        await message.reply_text("❌ <b>No active copy task is currently running.</b>")

@app.on_message(ALLOWED_FILTER & filters.command(["resume"], prefixes=["/", "."]))
async def resume_task(client: Client, message: Message):
    global is_paused, task_running
    if task_running and is_paused:
        is_paused = False
        await message.reply_text("▶️ <b>Task Resumed. Continuing message processing...</b>")
    elif not task_running:
        saved = get_progress()
        if saved and saved[6] in ["PAUSED", "RUNNING"]:
            source_chat, dest_chat, start_id, current_id, last_id, copied_count, _ = saved
            is_paused = False
            asyncio.create_task(
                run_copy_process(client, message, source_chat, dest_chat, start_id, current_id, last_id, copied_count)
            )
            await message.reply_text(f"▶️ <b>Resuming task from Checkpoint Message ID:</b> <code>{current_id}</code>")
        else:
            await message.reply_text("❌ <b>No paused or incomplete task found in database.</b>")

@app.on_message(ALLOWED_FILTER & filters.command(["status"], prefixes=["/", "."]))
async def status_command(client: Client, message: Message):
    await send_status_view(message)

async def send_status_view(target_ctx: Message | CallbackQuery):
    saved = get_progress()
    target_config = get_config("target_chat", "❌ Not Configured")
    brand = get_config("brand_name", "@skillneast1")

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏸️ Pause", callback_data="btn_pause"),
            InlineKeyboardButton("▶️ Resume", callback_data="btn_resume")
        ],
        [
            InlineKeyboardButton("🔄 Refresh Status", callback_data="btn_status")
        ]
    ])

    if task_running or (saved and saved[6] in ["RUNNING", "PAUSED"]):
        source_chat, dest_chat, start_id, current_id, last_id, copied_count, status = saved
        total_msgs = max(1, (last_id - start_id) + 1)
        processed_msgs = max(0, min(total_msgs, (current_id - start_id) + 1))
        percentage = round((processed_msgs / total_msgs) * 100, 1)
        progress_bar = generate_progress_bar(percentage)
        remaining = max(0, last_id - current_id)
        current_state = "PAUSED ⏸️" if is_paused else "ACTIVE 🟢"

        status_text = (
            "<b>📊 Live Task Progress Dashboard</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 <b>Source Channel:</b> <code>{source_chat}</code>\n"
            f"🎯 <b>Target Channel:</b> <code>{dest_chat}</code>\n"
            f"🎨 <b>Brand Tag:</b> <code>{brand}</code>\n"
            f"🔢 <b>Current Message ID:</b> <code>{current_id}</code>\n"
            f"🏁 <b>Target End ID:</b> <code>{last_id}</code>\n"
            f"📈 <b>Total Messages in Range:</b> <code>{total_msgs}</code>\n"
            f"✅ <b>Successfully Copied:</b> <code>{copied_count}</code>\n"
            f"⏳ <b>Remaining Messages:</b> <code>{remaining}</code>\n"
            f"⚡ <b>Execution State:</b> <code>{current_state}</code>\n\n"
            f"<b>Progress:</b> <code>[{progress_bar}]</code> <b>{percentage}%</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )
    else:
        status_text = (
            "<b>📊 Live Task Progress Dashboard</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "ℹ️ <i>No task is actively running.</i>\n\n"
            f"🎯 <b>Configured Target:</b> <code>{target_config}</code>\n"
            f"🎨 <b>Active Brand:</b> <code>{brand}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )

    if isinstance(target_ctx, Message):
        await target_ctx.reply_text(status_text, reply_markup=keyboard)
    elif isinstance(target_ctx, CallbackQuery):
        try:
            await target_ctx.message.edit_text(status_text, reply_markup=keyboard)
        except Exception:
            pass
        await target_ctx.answer("Status Updated")

@app.on_callback_query()
async def handle_callbacks(client: Client, callback: CallbackQuery):
    global is_paused, task_running
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
            if saved and saved[6] in ["PAUSED", "RUNNING"]:
                source_chat, dest_chat, start_id, current_id, last_id, copied_count, _ = saved
                is_paused = False
                asyncio.create_task(
                    run_copy_process(client, callback.message, source_chat, dest_chat, start_id, current_id, last_id, copied_count)
                )
                await callback.answer("Resumed from checkpoint!", show_alert=False)
                await send_status_view(callback)
            else:
                await callback.answer("No saved checkpoint found.", show_alert=True)
        else:
            await callback.answer("Task already running.", show_alert=True)

    elif data == "btn_brand":
        brand = get_config("brand_name", "@skillneast1")
        await callback.answer(f"Active Brand: {brand}", show_alert=True)

@app.on_message(ALLOWED_FILTER & filters.command(["copy"], prefixes=["/", "."]))
async def start_copy_command(client: Client, message: Message):
    global task_running, is_paused

    if task_running:
        await message.reply_text("⚠️ <b>A task is already running!</b> Use <code>/status</code> or <code>/pause</code>.")
        return

    dest_chat = get_config("target_chat")
    if not dest_chat:
        await message.reply_text("❌ <b>Target Channel Not Configured!</b>\nSet it using: <code>/settarget &lt;channel_id&gt;</code>")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.reply_text(
            "❌ <b>Usage:</b> <code>/copy &lt;telegram_link&gt;</code>\n"
            "Example: <code>/copy https://t.me/c/4429284952/134062-134080</code>"
        )
        return

    source_chat, start_msg_id, end_msg_id = parse_telegram_link(args[1])
    if not source_chat or not start_msg_id or not end_msg_id:
        await message.reply_text("❌ <b>Invalid Telegram Link!</b> Please supply a valid range or single message link.")
        return

    total_msgs = (end_msg_id - start_msg_id) + 1
    brand = get_config("brand_name", "@skillneast1")
    is_paused = False
    save_progress(str(source_chat), str(dest_chat), start_msg_id, start_msg_id, end_msg_id, 0, "RUNNING")

    start_text = (
        "<b>🚀 Initializing Range Copy Process</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Source Channel:</b> <code>{source_chat}</code>\n"
        f"🎯 <b>Target Channel:</b> <code>{dest_chat}</code>\n"
        f"🎨 <b>Brand Tag:</b> <code>{brand}</code>\n"
        f"🔢 <b>Start Message ID:</b> <code>{start_msg_id}</code>\n"
        f"🏁 <b>End Message ID:</b> <code>{end_msg_id}</code>\n"
        f"📦 <b>Total Messages to Process:</b> <code>{total_msgs}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<i>Processing messages continuously...</i>"
    )
    await message.reply_text(start_text)

    asyncio.create_task(
        run_copy_process(client, message, source_chat, dest_chat, start_msg_id, start_msg_id, end_msg_id, 0)
    )

# ==================== MAIN WORKER LOOP ====================
async def run_copy_process(
    client: Client,
    notify_message: Message,
    source_chat: int | str,
    dest_chat: int | str,
    start_id: int,
    current_start: int,
    last_id: int,
    initial_copied_count: int,
):
    global task_running, is_paused
    task_running = True
    is_paused = False

    # Resolve Chat Objects & Peer Hashes
    try:
        source_chat_obj = await client.get_chat(source_chat)
        dest_chat_obj = await client.get_chat(dest_chat)
    except PeerIdInvalid:
        await sync_dialogs(client)
        try:
            source_chat_obj = await client.get_chat(source_chat)
            dest_chat_obj = await client.get_chat(dest_chat)
        except Exception as e:
            await notify_message.reply_text(f"❌ <b>Peer Error:</b> <code>{e}</code>\nCheck if your account is a member in source and admin in target.")
            task_running = False
            return
    except Exception as e:
        await notify_message.reply_text(f"❌ <b>Chat Access Error:</b> <code>{e}</code>")
        task_running = False
        return

    # Pre-fetch Destination Raw Peer and Cover Photo
    dest_peer = await client.resolve_peer(dest_chat_obj.id)
    cover_photo = await get_or_upload_cover_photo(client)

    current_id = current_start
    copied_count = initial_copied_count
    total_msgs = max(1, (last_id - start_id) + 1)

    while current_id <= last_id:
        while is_paused:
            save_progress(str(source_chat), str(dest_chat), start_id, current_id, last_id, copied_count, "PAUSED")
            await asyncio.sleep(2)

        try:
            msg: Message = await client.get_messages(source_chat_obj.id, current_id)
            if msg and not msg.empty and not msg.service:
                raw_caption = msg.caption or msg.text or ""
                final_caption = process_caption(raw_caption)

                try:
                    # ==================== VIDEO_COVER & SMART THUMBNAIL LOGIC ====================
                    if msg.video:
                        # Check strictly if the original video actually has an original thumbnail
                        has_original_thumb = bool(msg.video.thumbs or msg.video.thumbnail)

                        if has_original_thumb and cover_photo:
                            # Apply custom cover photo instantly without downloading video
                            success = await send_video_with_instant_cover(
                                client=client,
                                dest_peer=dest_peer,
                                msg=msg,
                                caption=final_caption,
                                cover_photo=cover_photo,
                            )
                            if not success:
                                # Fallback if raw MTProto cover fails
                                await client.copy_message(
                                    chat_id=dest_chat_obj.id,
                                    from_chat_id=source_chat_obj.id,
                                    message_id=msg.id,
                                    caption=final_caption,
                                )
                        else:
                            # If no original thumb exists or no custom thumb set, copy video as-is
                            await client.copy_message(
                                chat_id=dest_chat_obj.id,
                                from_chat_id=source_chat_obj.id,
                                message_id=msg.id,
                                caption=final_caption,
                            )
                    else:
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
                    # ==================== FALLBACK: DOWNLOAD & RE-UPLOAD ====================
                    if msg.media:
                        file_path = await client.download_media(msg)
                        if file_path:
                            try:
                                if msg.video:
                                    has_original_thumb = bool(msg.video.thumbs or msg.video.thumbnail)
                                    thumb_to_use = CUSTOM_THUMB_PATH if (has_original_thumb and os.path.exists(CUSTOM_THUMB_PATH)) else None
                                    await client.send_video(
                                        chat_id=dest_chat_obj.id,
                                        video=file_path,
                                        caption=final_caption,
                                        thumb=thumb_to_use,
                                    )
                                elif msg.photo:
                                    await client.send_photo(dest_chat_obj.id, photo=file_path, caption=final_caption)
                                elif msg.document:
                                    await client.send_document(dest_chat_obj.id, document=file_path, caption=final_caption)
                                elif msg.audio:
                                    await client.send_audio(dest_chat_obj.id, audio=file_path, caption=final_caption)
                                elif msg.animation:
                                    await client.send_animation(dest_chat_obj.id, animation=file_path, caption=final_caption)
                                copied_count += 1
                            finally:
                                if os.path.exists(file_path):
                                    os.remove(file_path)

                await asyncio.sleep(DELAY_SECONDS)

            current_id += 1
            save_progress(str(source_chat), str(dest_chat), start_id, current_id, last_id, copied_count, "RUNNING")

        except (ChatWriteForbidden, ChatAdminRequired):
            await notify_message.reply_text(
                f"❌ <b>Permission Denied!</b> Ensure your account is an <b>ADMIN</b> with post permissions in: <code>{dest_chat}</code>"
            )
            task_running = False
            save_progress(str(source_chat), str(dest_chat), start_id, current_id, last_id, copied_count, "STOPPED")
            return

        except FloodWait as e:
            await notify_message.reply_text(f"⏳ <b>FloodWait Encountered:</b> Sleeping for {e.value} seconds...")
            await asyncio.sleep(e.value)
        except Exception:
            current_id += 1
            await asyncio.sleep(1)

    task_running = False
    save_progress(str(source_chat), str(dest_chat), start_id, current_id, last_id, copied_count, "COMPLETED")

    completed_text = (
        "<b>🎉 Range Copy Task Completed!</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📦 <b>Total Messages in Range:</b> <code>{total_msgs}</code>\n"
        f"✅ <b>Successfully Copied:</b> <code>{copied_count}</code>\n"
        f"🎯 <b>Target Channel:</b> <code>{dest_chat}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    await notify_message.reply_text(completed_text)

# ==================== RUNNER ====================
async def main():
    await app.start()
    print("⚡ Syncing dialogs into peer cache...")
    await sync_dialogs(app)
    print("✅ Userbot is fully connected and ready!")
    await start_web_server()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
