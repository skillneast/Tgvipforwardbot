import asyncio
import os
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
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

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
DB_NAME = "railway_live_dashboard.db"

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

def save_progress(source_chat: str, dest_chat: str, start_id: int, current_id: int, last_id: int, copied_count: int, status: str):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO task_state (id, source_chat, dest_chat, start_id, current_id, last_id, copied_count, status)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?)
    """, (str(source_chat), str(dest_chat), start_id, current_id, last_id, copied_count, status))
    conn.commit()
    conn.close()

def get_progress():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT source_chat, dest_chat, start_id, current_id, last_id, copied_count, status FROM task_state WHERE id = 1")
    row = cursor.fetchone()
    conn.close()
    return row

init_db()

# ==================== PYROGRAM CLIENT ====================
app = Client(
    "railway_dashboard_session",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

task_running = False
is_paused = False
task_cancelled = False
task_start_time = 0.0

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

def render_dashboard(
    source_chat: str,
    dest_chat: str,
    brand: str,
    start_id: int,
    current_id: int,
    last_id: int,
    copied_count: int,
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
        "<b>🚀 REAL-TIME COPY DASHBOARD</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Source Chat:</b> <code>{source_chat}</code>\n"
        f"🎯 <b>Target Chat:</b> <code>{dest_chat}</code>\n"
        f"🎨 <b>Brand Watermark:</b> <code>{brand}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>PROGRESS:</b> <code>[{bar}]</code> <b>{percentage}%</b>\n\n"
        f"🔢 <b>Current Message ID:</b> <code>{current_id}</code> / <code>{last_id}</code>\n"
        f"📦 <b>Total in Range:</b> <code>{total_msgs}</code> msgs\n"
        f"✅ <b>Successfully Copied:</b> <code>{copied_count}</code>\n"
        f"⏳ <b>Remaining to Process:</b> <code>{remaining_msgs}</code> msgs\n"
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
            InlineKeyboardButton("🔄 Live Refresh", callback_data="btn_status"),
            InlineKeyboardButton("🛑 Cancel", callback_data="btn_stop")
        ]
    ])

    return card, keyboard

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

# ==================== DUMMY WEB SERVER ====================
web_app = FastAPI()

@web_app.get("/")
@web_app.get("/health")
async def health_check():
    return JSONResponse(
        status_code=200,
        content={
            "status": "online",
            "task_running": task_running,
            "is_paused": is_paused
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
        "• <code>/copy &lt;link&gt;</code> — Start copy directly with Real-Time Dashboard\n"
        "• <code>/settarget &lt;id&gt;</code> — Set destination channel ID\n"
        "• <code>/setbrand &lt;name&gt;</code> — Change caption brand watermark\n"
        "• <code>/getbrand</code> — Check current brand name\n"
        "• <code>/status</code> — Open Live Dashboard view\n"
        "• <code>/pause</code> | <code>/resume</code> — Process control\n"
        "• <code>/sync</code> — Sync channel dialogs & access hashes\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Dashboard", callback_data="btn_status"),
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

@app.on_message(ALLOWED_FILTER & filters.command(["pause"], prefixes=["/", "."]))
async def pause_task(client: Client, message: Message):
    global is_paused
    if task_running and not is_paused:
        is_paused = True
        await message.reply_text("⏸️ <b>Task Paused.</b> Use <code>/resume</code> to continue.")
    else:
        await message.reply_text("❌ <b>No active copy task running.</b>")

@app.on_message(ALLOWED_FILTER & filters.command(["resume"], prefixes=["/", "."]))
async def resume_task(client: Client, message: Message):
    global is_paused, task_running, task_start_time, task_cancelled
    if task_running and is_paused:
        is_paused = False
        await message.reply_text("▶️ <b>Task Resumed. Continuing message processing...</b>")
    elif not task_running:
        saved = get_progress()
        if saved and saved[6] in ["PAUSED", "RUNNING"]:
            source_chat, dest_chat, start_id, current_id, last_id, copied_count, _ = saved
            is_paused = False
            task_cancelled = False
            task_start_time = time.time()
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

    if task_running or (saved and saved[6] in ["RUNNING", "PAUSED"]):
        source_chat, dest_chat, start_id, current_id, last_id, copied_count, status = saved
        status_label = "PAUSED ⏸️" if is_paused else "RUNNING 🟢"
        card_text, keyboard = render_dashboard(
            source_chat=source_chat,
            dest_chat=dest_chat,
            brand=brand,
            start_id=start_id,
            current_id=current_id,
            last_id=last_id,
            copied_count=copied_count,
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
        await target_ctx.reply_text(card_text, reply_markup=keyboard, disable_web_page_preview=True)
    elif isinstance(target_ctx, CallbackQuery):
        try:
            await target_ctx.message.edit_text(card_text, reply_markup=keyboard, disable_web_page_preview=True)
        except (MessageNotModified, Exception):
            pass
        await target_ctx.answer("Refreshed")

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
            if saved and saved[6] in ["PAUSED", "RUNNING"]:
                source_chat, dest_chat, start_id, current_id, last_id, copied_count, _ = saved
                is_paused = False
                task_cancelled = False
                task_start_time = time.time()
                asyncio.create_task(
                    run_copy_process(client, callback.message, source_chat, dest_chat, start_id, current_id, last_id, copied_count)
                )
                await callback.answer("Resumed from checkpoint!", show_alert=False)
                await send_status_view(callback)
            else:
                await callback.answer("No saved checkpoint found.", show_alert=True)
        else:
            await callback.answer("Task already running.", show_alert=True)

    elif data == "btn_stop":
        if task_running:
            task_cancelled = True
            task_running = False
            await callback.answer("🛑 Task Cancelled", show_alert=True)
            await send_status_view(callback)
        else:
            await callback.answer("No active task to stop.", show_alert=True)

    elif data == "btn_brand":
        brand = get_config("brand_name", "@skillneast1")
        await callback.answer(f"Active Brand: {brand}", show_alert=True)

# ==================== MAIN COPY COMMAND ====================
@app.on_message(ALLOWED_FILTER & filters.command(["copy"], prefixes=["/", "."]))
async def start_copy_command(client: Client, message: Message):
    global task_running, is_paused, task_cancelled, task_start_time

    if task_running:
        await message.reply_text("⚠️ <b>A task is already running!</b> Use <code>/pause</code> or wait for completion.")
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
    save_progress(str(source_chat), str(dest_chat), start_msg_id, start_msg_id, end_msg_id, 0, "RUNNING")

    # Launch worker which sends the LIVE DASHBOARD instantly
    asyncio.create_task(
        run_copy_process(client, message, source_chat, dest_chat, start_msg_id, start_msg_id, end_msg_id, 0)
    )

# ==================== CORE WORKER WITH REAL-TIME DASHBOARD ====================
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
    global task_running, is_paused, task_cancelled, task_start_time
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

    brand = get_config("brand_name", "@skillneast1")
    current_id = current_start
    copied_count = initial_copied_count

    # 1. SEND LIVE DASHBOARD DIRECTLY ON /copy
    initial_card, initial_keyboard = render_dashboard(
        source_chat=str(source_chat),
        dest_chat=str(dest_chat),
        brand=brand,
        start_id=start_id,
        current_id=current_id,
        last_id=last_id,
        copied_count=copied_count,
        status_label="RUNNING 🟢",
        start_time=task_start_time,
    )
    dashboard_msg: Message = await notify_message.reply_text(
        initial_card,
        reply_markup=initial_keyboard,
        disable_web_page_preview=True,
    )

    last_dashboard_edit_time = time.time()

    while current_id <= last_id:
        if task_cancelled:
            save_progress(str(source_chat), str(dest_chat), start_id, current_id, last_id, copied_count, "CANCELLED")
            await notify_message.reply_text("🛑 <b>Task cancelled by user.</b>")
            task_running = False
            return

        while is_paused:
            save_progress(str(source_chat), str(dest_chat), start_id, current_id, last_id, copied_count, "PAUSED")
            await asyncio.sleep(2)

        try:
            msg: Message = await client.get_messages(source_chat_obj.id, current_id)
            if msg and not msg.empty and not msg.service:
                raw_caption = msg.caption or msg.text or ""
                final_caption = process_caption(raw_caption)

                try:
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
                    if msg.media:
                        file_path = await client.download_media(msg)
                        if file_path:
                            try:
                                if msg.video:
                                    await client.send_video(dest_chat_obj.id, video=file_path, caption=final_caption)
                                elif msg.photo:
                                    await client.send_photo(dest_chat_obj.id, photo=file_path, caption=final_caption)
                                elif msg.document:
                                    await client.send_document(dest_chat_obj.id, document=file_path, caption=final_caption)
                                elif msg.audio:
                                    await client.send_audio(dest_chat_obj.id, audio=file_path, caption=final_caption)
                                copied_count += 1
                            finally:
                                if os.path.exists(file_path):
                                    os.remove(file_path)

                await asyncio.sleep(DELAY_SECONDS)

            current_id += 1
            save_progress(str(source_chat), str(dest_chat), start_id, current_id, last_id, copied_count, "RUNNING")

            # 2. REAL-TIME DASHBOARD AUTO-EDIT (Every 3-4 seconds)
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
                    status_label="RUNNING 🟢",
                    start_time=task_start_time,
                )
                try:
                    await dashboard_msg.edit_text(
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
            save_progress(str(source_chat), str(dest_chat), start_id, current_id, last_id, copied_count, "STOPPED")
            return

        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception:
            current_id += 1
            await asyncio.sleep(1)

    task_running = False
    save_progress(str(source_chat), str(dest_chat), start_id, current_id, last_id, copied_count, "COMPLETED")

    # 3. FINAL COMPLETED DASHBOARD VIEW
    completed_card, completed_keyboard = render_dashboard(
        source_chat=str(source_chat),
        dest_chat=str(dest_chat),
        brand=brand,
        start_id=start_id,
        current_id=last_id,
        last_id=last_id,
        copied_count=copied_count,
        status_label="COMPLETED 🎉",
        start_time=task_start_time,
    )
    try:
        await dashboard_msg.edit_text(
            completed_card,
            reply_markup=completed_keyboard,
            disable_web_page_preview=True
        )
    except Exception:
        await notify_message.reply_text(completed_card, reply_markup=completed_keyboard)

# ==================== RUNNER ====================
async def main():
    await app.start()
    print("⚡ Syncing dialogs into peer cache...")
    await sync_dialogs(app)
    print("✅ Live Dashboard Userbot is Online & Ready on Railway!")
    await start_web_server()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
