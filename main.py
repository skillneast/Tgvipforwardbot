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
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

nest_asyncio.apply()

# ==================== CONFIGURATION & ENVIRONMENT VARIABLES ====================
API_ID_RAW = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
SESSION_STRING = os.environ.get("SESSION_STRING")
DELAY_SECONDS = int(os.environ.get("DELAY_SECONDS", 3))
PORT = int(os.environ.get("PORT", 8080))

if not API_ID_RAW or not API_HASH or not SESSION_STRING:
    raise ValueError("Missing critical Environment Variables: API_ID, API_HASH, or SESSION_STRING")

API_ID = int(API_ID_RAW)
DB_NAME = "userbot_data.db"
CUSTOM_THUMB_PATH = "custom_thumb.jpg"

# ==================== DATABASE INITIALIZATION & ACCESSORS ====================
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
    # Set default brand name if not already present
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('brand_name', '@skillneast1')")
    conn.commit()
    conn.close()

def get_config_val(key: str, default: Optional[str] = None) -> Optional[str]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM config WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default

def set_config_val(key: str, value: str):
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

# ==================== PYROGRAM CLIENT & GLOBAL STATE ====================
app = Client(
    "render_userbot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

task_running = False
is_paused = False
current_worker_task: Optional[asyncio.Task] = None

# ==================== HELPER FUNCTIONS ====================
def generate_progress_bar(percentage: float, length: int = 10) -> str:
    filled = int(round(length * (percentage / 100)))
    filled = max(0, min(length, filled))
    empty = length - filled
    return "█" * filled + "░" * empty

def process_caption(caption_text: str) -> str:
    brand = get_config_val("brand_name", "@skillneast1")
    if not caption_text:
        return f"Provided by {brand}"

    usernames = re.findall(r"@[a-zA-Z0-9_]+", caption_text)
    if usernames:
        return re.sub(r"@[a-zA-Z0-9_]+", brand, caption_text)
    else:
        return f"{caption_text}\n\nProvided by {brand}"

def parse_telegram_link(link: str) -> Tuple[Optional[int | str], Optional[int], Optional[int]]:
    link = link.strip()
    # Private Channel Range
    p_range = re.search(r"t\.me/c/(\d+)/(\d+)-(\d+)", link)
    if p_range:
        return int("-100" + p_range.group(1)), int(p_range.group(2)), int(p_range.group(3))

    # Private Channel Single
    p_single = re.search(r"t\.me/c/(\d+)/(\d+)", link)
    if p_single:
        start = int(p_single.group(2))
        return int("-100" + p_single.group(1)), start, start + 250000

    # Public Channel Range
    pub_range = re.search(r"t\.me/([^/]+)/(\d+)-(\d+)", link)
    if pub_range:
        return pub_range.group(1), int(pub_range.group(2)), int(pub_range.group(3))

    # Public Channel Single
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

# ==================== DUMMY WEB SERVER (RENDER HEALTH CHECK) ====================
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
            "service": "Telegram Userbot"
        }
    )

async def start_web_server():
    config = uvicorn.Config(app=web_app, host="0.0.0.0", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()

# ==================== COMMAND HANDLERS & INLINE MENUS ====================
ALLOWED_FILTER = (filters.me | filters.private)

@app.on_message(ALLOWED_FILTER & filters.command(["start", "help"], prefixes=["/", "."]))
async def start_command(client: Client, message: Message):
    target = get_config_val("target_chat", "❌ Not Set")
    brand = get_config_val("brand_name", "@skillneast1")

    welcome_text = (
        "<b>🤖 Personal Content Forwarder Userbot</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Target Channel:</b> <code>{target}</code>\n"
        f"🎨 <b>Current Brand:</b> <code>{brand}</code>\n\n"
        "<b>📖 Available Commands:</b>\n"
        "• <code>/copy &lt;link&gt;</code> — Start moving/copying messages\n"
        "• <code>/settarget &lt;chat_id&gt;</code> — Configure target channel\n"
        "• <code>/setbrand &lt;name&gt;</code> — Update caption brand name\n"
        "• <code>/getbrand</code> — View active brand\n"
        "• <code>/setthumb</code> — Reply to image to save as video thumbnail\n"
        "• <code>/status</code> — Visual progress & task details\n"
        "• <code>/pause</code> | <code>/resume</code> — Process state control\n"
        "• <code>/sync</code> — Reload Telegram peer access caches\n"
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
        current = get_config_val("brand_name", "@skillneast1")
        await message.reply_text(f"ℹ️ Current Brand Name: <code>{current}</code>\n\nUsage: <code>/setbrand &lt;new_brand_or_username&gt;</code>")
        return

    old_brand = get_config_val("brand_name", "@skillneast1")
    new_brand = args[1].strip()
    set_config_val("brand_name", new_brand)

    await message.reply_text(
        f"<b>✅ Brand Name Updated Successfully!</b>\n\n"
        f"<b>Old:</b> <code>{old_brand}</code>\n"
        f"<b>New:</b> <code>{new_brand}</code>"
    )

@app.on_message(ALLOWED_FILTER & filters.command(["getbrand", "brand"], prefixes=["/", "."]))
async def get_brand_command(client: Client, message: Message):
    brand = get_config_val("brand_name", "@skillneast1")
    await message.reply_text(f"🎨 <b>Current Brand:</b> <code>{brand}</code>")

@app.on_message(ALLOWED_FILTER & filters.command(["sync"], prefixes=["/", "."]))
async def sync_command(client: Client, message: Message):
    status_msg = await message.reply_text("🔄 <i>Syncing joined channels and resolving access hashes...</i>")
    success = await sync_dialogs(client)
    if success:
        await status_msg.edit_text("<b>✅ All dialogs & peer hashes synced successfully!</b>")
    else:
        await status_msg.edit_text("<b>⚠️ Sync completed with non-fatal warnings.</b>")

@app.on_message(ALLOWED_FILTER & filters.command(["settarget"], prefixes=["/", "."]))
async def set_target_cmd(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.reply_text("❌ <b>Error:</b> Target chat missing.\nUsage: <code>/settarget -100xxxxxxxxxx</code>")
        return

    target_chat = args[1].strip()
    set_config_val("target_chat", target_chat)
    await message.reply_text(
        f"<b>✅ Target Channel Configured!</b>\n\n"
        f"🎯 <b>Channel ID:</b> <code>{target_chat}</code>\n"
        f"<i>Ensure you have admin posting permissions in this channel.</i>"
    )

@app.on_message(ALLOWED_FILTER & filters.command(["setthumb"], prefixes=["/", "."]))
async def set_thumb(client: Client, message: Message):
    if message.reply_to_message and message.reply_to_message.photo:
        status_msg = await message.reply_text("📥 <i>Downloading and saving custom thumbnail...</i>")
        await client.download_media(message.reply_to_message.photo, file_name=CUSTOM_THUMB_PATH)
        await status_msg.edit_text(
            "<b>✅ Custom Thumbnail Saved!</b>\n\n"
            "⚠️ <i>Note: This will only be applied to videos that ALREADY have an original thumbnail. "
            "Videos without thumbnails will stay completely unchanged.</i>"
        )
    else:
        await message.reply_text("❌ <b>Error:</b> Please reply to an image with <code>/setthumb</code>.")

@app.on_message(ALLOWED_FILTER & filters.command(["pause"], prefixes=["/", "."]))
async def pause_task(client: Client, message: Message):
    global is_paused
    if task_running and not is_paused:
        is_paused = True
        await message.reply_text("⏸️ <b>Task Paused Successfully.</b>\nUse <code>/resume</code> or inline buttons to continue.")
    else:
        await message.reply_text("❌ <b>No active copy task is currently running.</b>")

@app.on_message(ALLOWED_FILTER & filters.command(["resume"], prefixes=["/", "."]))
async def resume_task(client: Client, message: Message):
    global is_paused, task_running, current_worker_task
    if task_running and is_paused:
        is_paused = False
        await message.reply_text("▶️ <b>Task Resumed. Continuing message processing...</b>")
    elif not task_running:
        saved = get_progress()
        if saved and saved[6] in ["PAUSED", "RUNNING"]:
            source_chat, dest_chat, start_id, current_id, last_id, copied_count, _ = saved
            is_paused = False
            current_worker_task = asyncio.create_task(
                run_copy_process(client, message, source_chat, dest_chat, start_id, current_id, last_id, copied_count)
            )
            await message.reply_text(f"▶️ <b>Resuming task from Message ID:</b> <code>{current_id}</code>")
        else:
            await message.reply_text("❌ <b>No paused or incomplete task found in database.</b>")

@app.on_message(ALLOWED_FILTER & filters.command(["status"], prefixes=["/", "."]))
async def status_command(client: Client, message: Message):
    await send_status_view(message)

async def send_status_view(target_ctx: Message | CallbackQuery):
    saved = get_progress()
    target_config = get_config_val("target_chat", "❌ Not Set")
    brand = get_config_val("brand_name", "@skillneast1")

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
            "<b>📊 Task Execution Status</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 <b>Source:</b> <code>{source_chat}</code>\n"
            f"🎯 <b>Target:</b> <code>{dest_chat}</code>\n"
            f"🎨 <b>Brand:</b> <code>{brand}</code>\n"
            f"🔢 <b>Current Message:</b> <code>{current_id}</code>\n"
            f"🏁 <b>Target End:</b> <code>{last_id}</code>\n"
            f"📦 <b>Total Copied:</b> <code>{copied_count}</code>\n"
            f"⏳ <b>Remaining:</b> <code>{remaining}</code> msgs\n"
            f"⚡ <b>State:</b> <code>{current_state}</code>\n\n"
            f"<b>Progress:</b> <code>[{progress_bar}]</code> <b>{percentage}%</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )
    else:
        status_text = (
            "<b>📊 Task Execution Status</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "ℹ️ <i>No task is actively running.</i>\n\n"
            f"🎯 <b>Configured Target:</b> <code>{target_config}</code>\n"
            f"🎨 <b>Configured Brand:</b> <code>{brand}</code>\n"
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
    global is_paused, task_running, current_worker_task
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
                current_worker_task = asyncio.create_task(
                    run_copy_process(client, callback.message, source_chat, dest_chat, start_id, current_id, last_id, copied_count)
                )
                await callback.answer("Resumed from checkpoint!", show_alert=False)
                await send_status_view(callback)
            else:
                await callback.answer("No saved checkpoint found.", show_alert=True)
        else:
            await callback.answer("Task already running.", show_alert=True)

    elif data == "btn_brand":
        brand = get_config_val("brand_name", "@skillneast1")
        await callback.answer(f"Current Brand: {brand}", show_alert=True)

@app.on_message(ALLOWED_FILTER & filters.command(["copy"], prefixes=["/", "."]))
async def start_copy_command(client: Client, message: Message):
    global task_running, is_paused, current_worker_task

    if task_running:
        await message.reply_text("⚠️ <b>A task is already running!</b> Use <code>/status</code> or <code>/pause</code>.")
        return

    dest_chat = get_config_val("target_chat")
    if not dest_chat:
        await message.reply_text("❌ <b>Target Channel Not Configured!</b>\nSet it using: <code>/settarget &lt;channel_id&gt;</code>")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.reply_text("❌ <b>Usage:</b> <code>/copy &lt;telegram_link&gt;</code>\nExample: <code>/copy https://t.me/c/1234567890/100-200</code>")
        return

    source_chat, start_msg_id, end_msg_id = parse_telegram_link(args[1])
    if not source_chat or not start_msg_id or not end_msg_id:
        await message.reply_text("❌ <b>Invalid Telegram Message Link!</b> Please supply a valid single or range link.")
        return

    is_paused = False
    save_progress(str(source_chat), str(dest_chat), start_msg_id, start_msg_id, end_msg_id, 0, "RUNNING")

    start_text = (
        "<b>🚀 Initializing Task Execution</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Source:</b> <code>{source_chat}</code>\n"
        f"🎯 <b>Target:</b> <code>{dest_chat}</code>\n"
        f"🔢 <b>Start ID:</b> <code>{start_msg_id}</code>\n"
        f"🏁 <b>End ID:</b> <code>{end_msg_id}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    await message.reply_text(start_text)

    current_worker_task = asyncio.create_task(
        run_copy_process(client, message, source_chat, dest_chat, start_msg_id, start_msg_id, end_msg_id, 0)
    )

# ==================== CORE ASYNC WORKER LOOP ====================
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

    # Resolve Peers
    try:
        source_chat_obj = await client.get_chat(source_chat)
        dest_chat_obj = await client.get_chat(dest_chat)
    except PeerIdInvalid:
        await sync_dialogs(client)
        try:
            source_chat_obj = await client.get_chat(source_chat)
            dest_chat_obj = await client.get_chat(dest_chat)
        except Exception as e:
            await notify_message.reply_text(f"❌ <b>Peer Error:</b> <code>{e}</code>\nCheck if account is a member/admin in the chats.")
            task_running = False
            return
    except Exception as e:
        await notify_message.reply_text(f"❌ <b>Chat Access Error:</b> <code>{e}</code>")
        task_running = False
        return

    current_id = current_start
    copied_count = initial_copied_count

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
                    # ==================== SMART THUMBNAIL LOGIC ====================
                    if msg.video:
                        # Strictly verify if the original video actually possessed a thumbnail
                        has_original_thumb = bool(msg.video.thumbs or msg.video.thumbnail)

                        if has_original_thumb and os.path.exists(CUSTOM_THUMB_PATH):
                            # Replace thumbnail on existing cloud video file without downloading
                            await client.send_video(
                                chat_id=dest_chat_obj.id,
                                video=msg.video.file_id,
                                caption=final_caption,
                                thumb=CUSTOM_THUMB_PATH,
                            )
                        else:
                            # Forward/copy directly as-is (thumbnail-less or no custom thumbnail saved)
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
                f"❌ <b>Permission Denied!</b> Ensure the account is an <b>ADMIN</b> with full posting rights in target: <code>{dest_chat}</code>"
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
    await notify_message.reply_text(f"<b>✅ Copy Task Completed Successfully!</b>\nTotal Messages Processed: <code>{copied_count}</code>")

# ==================== MAIN APPLICATION RUNNER ====================
async def main():
    # Start Pyrogram userbot client
    await app.start()
    print("⚡ Syncing dialogs into peer cache...")
    await sync_dialogs(app)
    print("✅ Userbot is fully connected and ready!")

    # Start FastAPI Web Server concurrently in the same asyncio loop
    await start_web_server()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
