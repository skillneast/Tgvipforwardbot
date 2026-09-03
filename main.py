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
SESSION_STRING = os.environ.get("SESSION_STRING", "")

if not SESSION_STRING:
    raise ValueError("Missing critical Environment Variable: SESSION_STRING")

DELAY_SECONDS = float(os.environ.get("DELAY_SECONDS", 1.0))
PORT = int(os.environ.get("PORT", 8080))
DB_NAME = "ultimate_dual_mode_v4.db"

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
            texts_count INTEGER,
            branded_count INTEGER,
            status TEXT
        )
    """)
    defaults = {
        'brand_name': '@skillneast1',
        'caption_prefix': 'Extracted By',
        'target_chat': '',
        'mode2_target': '',
        'mode2_brand': '@skillneast1',
        'mode2_prefix': 'Extracted By',
        'brand_enabled': 'on',
        'brand_percentage': '40',
        'mode2_brand_enabled': 'on',
        'mode2_brand_percentage': '40',
        'mode2_live_active': 'off'
    }
    for k, v in defaults.items():
        cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v))
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
    texts_count: int,
    branded_count: int,
    status: str,
):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO task_state (
            id, source_chat, dest_chat, start_id, current_id, last_id,
            copied_count, videos_count, texts_count, branded_count, status
        )
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        str(source_chat), str(dest_chat), start_id, current_id, last_id,
        copied_count, videos_count, texts_count, branded_count, status
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
               copied_count, videos_count, texts_count, branded_count, status
        FROM task_state WHERE id = 1
    """)
    row = cursor.fetchone()
    conn.close()
    return row

init_db()

# ==================== PYROGRAM CLIENT ====================
app = Client(
    "ultimate_dual_mode_v4_session",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

task_running = False
is_paused = False
task_cancelled = False
task_start_time = 0.0
active_dashboard_msg: Optional[Message] = None

# ==================== UI & SMART QUOTED-CAPTION LOGIC ====================
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

def process_caption_custom(
    caption_text: str,
    brand: str,
    prefix: str,
    enabled: str,
    percentage_val: int,
    is_pure_text: bool = False
) -> Tuple[str, bool]:
    if enabled.lower() == "off":
        return caption_text if caption_text else "", False

    if not caption_text:
        if random.random() < (percentage_val / 100.0):
            return f"{prefix} ➤ {brand}", True
        return "", False

    # 1. 100% Replace existing @usernames (Even inside Quotes/Blocks)
    usernames = re.findall(r"@[a-zA-Z0-9_]+", caption_text)
    if usernames:
        new_cap = caption_text
        for u in usernames:
            new_cap = new_cap.replace(u, brand.split("➤")[-1].strip() if "➤" in brand else brand)
        return new_cap, True

    # 2. Smart Detection for Quoted or Normal "Extracted By : @MRANUJ7" or "Extracted By ➤ Name"
    pattern = re.compile(r'(extracted\s*by|downloaded\s*by|uploaded\s*by|creds\s*by|by)\s*[:➤—–-]\s*([^\n]+)', re.IGNORECASE)
    if pattern.search(caption_text):
        new_cap = pattern.sub(rf'\1 ➤ {brand.split("➤")[-1].strip() if "➤" in brand else brand}', caption_text)
        return new_cap, True

    # 3. Short titles protection
    if is_pure_text:
        clean_txt = caption_text.strip()
        if len(clean_txt) <= 30 or clean_txt.lower() in ["welcome", "complete", "notes", "index", "module"]:
            return caption_text, False

    # 4. Custom Percentage Roll (e.g. 40%)
    if random.random() < (percentage_val / 100.0):
        watermark_str = f"{prefix} ➤ {brand}".strip()
        return f"{caption_text}\n\n{watermark_str}", True

    return caption_text, False

def render_dashboard(
    mode_title: str,
    source_chat: str,
    dest_chat: str,
    brand: str,
    prefix: str,
    start_id: int,
    current_id: int,
    last_id: int,
    copied_count: int,
    videos_count: int,
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
        f"<b>🚀 {mode_title} LIVE DASHBOARD V4</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Source/Channel:</b> <code>{source_chat}</code>\n"
        f"🎯 <b>Target Destination:</b> <code>{dest_chat}</code>\n"
        f"🎨 <b>Active Watermark:</b> <code>{prefix} ➤ {brand}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>PROGRESS:</b> <code>[{bar}]</code> <b>{percentage}%</b>\n\n"
        f"🔢 <b>Current ID:</b> <code>{current_id}</code> / <code>{last_id}</code>\n"
        f"📦 <b>Total Range:</b> <code>{total_msgs}</code> msgs\n"
        f"✅ <b>Processed:</b> <code>{copied_count}</code>\n"
        f"⏳ <b>Remaining:</b> <code>{remaining_msgs}</code> msgs\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>📂 INSTANT STATS LOGGER:</b>\n"
        f"• 🎥 <b>Videos Processed:</b> <code>{videos_count}</code>\n"
        f"• 📝 <b>Texts/Files Processed:</b> <code>{texts_count}</code>\n"
        f"• 🏷️ <b>Auto-Watermarked:</b> <code>{branded_count}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱️ <b>Elapsed:</b> <code>{elapsed_str}</code> | ⌛ <b>ETA:</b> <code>{eta_str}</code>\n"
        f"⚡ <b>Speed:</b> <code>{speed_per_min} msgs/min</code>\n"
        f"📶 <b>State:</b> <code>{status_label}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏸️ Pause", callback_data="btn_pause"),
            InlineKeyboardButton("▶️ Resume", callback_data="btn_resume")
        ],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="btn_status"),
            InlineKeyboardButton("🛑 Cancel", callback_data="btn_stop")
        ]
    ])

    return card, keyboard

def parse_telegram_link(link: str) -> Tuple[Optional[int | str], Optional[int], Optional[int]]:
    link = link.strip()
    p_range_dash = re.search(r"t\.me/c/(\d+)/(\d+)-(\d+)", link)
    if p_range_dash:
        return int("-100" + p_range_dash.group(1)), int(p_range_dash.group(2)), int(p_range_dash.group(3))

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

# ==================== FASTAPI WEB SERVER ====================
web_app = FastAPI()

@web_app.get("/")
@web_app.get("/health")
async def health_check():
    return JSONResponse(status_code=200, content={"status": "online", "task_running": task_running})

async def start_web_server():
    config = uvicorn.Config(app=web_app, host="0.0.0.0", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()

# ==================== COMMAND HANDLERS ====================
ALLOWED_FILTER = (filters.me | filters.private)

@app.on_message(ALLOWED_FILTER & filters.command(["start", "help"], prefixes=["/", "."]))
async def start_command(client: Client, message: Message):
    target1 = get_config("target_chat", "❌ Not Set")
    target2 = get_config("mode2_target", "❌ Not Set")
    brand1 = get_config("brand_name", "@skillneast1")
    brand2 = get_config("mode2_brand", "@skillneast1")
    m2_live = get_config("mode2_live_active", "off")

    welcome_text = (
        "<b>🤖 Ultimate Dual-Mode Userbot V4 Active</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Mode 1 Target:</b> <code>{target1}</code> | Brand: <code>{brand1}</code>\n"
        f"🎯 <b>Mode 2 Target:</b> <code>{target2}</code> | Brand: <code>{brand2}</code>\n"
        f"⚡ <b>Mode 2 Live Auto-Watermark:</b> <code>{m2_live.upper()}</code>\n\n"
        "<b>📖 Commands:</b>\n"
        "• <code>/copy &lt;link&gt;</code> — Mode 1 (Copy & Paste to Target 1)\n"
        "• <code>/mode2 &lt;link&gt; &lt;start&gt;-&lt;end&gt;</code> — Mode 2 (Edit in-place in Target 2)\n"
        "• <code>/mode2live on</code> or <code>off</code> — Auto-watermark incoming files\n"
        "• <code>/settarget2 &lt;id&gt;</code> | <code>/setbrand2 &lt;name&gt;</code>\n"
        "• <code>/setpercentage 40</code> (or 100 for all files)\n"
        "• <code>/ld</code> (Live Dashboard) | <code>/cancel</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Live Dashboard", callback_data="btn_status"),
            InlineKeyboardButton("🛑 Cancel Task", callback_data="btn_stop")
        ]
    ])
    await message.reply_text(welcome_text, reply_markup=keyboard, disable_web_page_preview=True)

@app.on_message(ALLOWED_FILTER & filters.command(["cancel", "stop"], prefixes=["/", "."]))
async def cancel_command(client: Client, message: Message):
    global task_running, is_paused, task_cancelled
    task_cancelled = True
    task_running = False
    is_paused = False
    delete_task_progress()
    await message.reply_text("<b>🛑 Task Cancelled & Cleared Successfully!</b>")

@app.on_message(ALLOWED_FILTER & filters.command(["ld", "status"], prefixes=["/", "."]))
async def ld_command(client: Client, message: Message):
    await send_status_view(message)

async def send_status_view(target_ctx: Message | CallbackQuery):
    global active_dashboard_msg
    saved = get_progress()
    
    if task_running or (saved and saved[10] in ["RUNNING", "PAUSED"]):
        (
            source_chat, dest_chat, start_id, current_id, last_id,
            copied_count, videos_count, texts_count, branded_count, status
        ) = saved
        status_label = "PAUSED ⏸️" if is_paused else "RUNNING 🟢"
        
        mode_title = "MODE 2 (RANGE EDITOR)" if str(source_chat) == str(dest_chat) else "MODE 1 (COPY COPIER)"
        brand_used = get_config("mode2_brand") if mode_title.startswith("MODE 2") else get_config("brand_name")
        prefix_used = get_config("mode2_prefix") if mode_title.startswith("MODE 2") else get_config("caption_prefix")

        card_text, keyboard = render_dashboard(
            mode_title=mode_title,
            source_chat=source_chat,
            dest_chat=dest_chat,
            brand=brand_used,
            prefix=prefix_used,
            start_id=start_id,
            current_id=current_id,
            last_id=last_id,
            copied_count=copied_count,
            videos_count=videos_count,
            texts_count=texts_count,
            branded_count=branded_count,
            status_label=status_label,
            start_time=task_start_time,
        )
    else:
        card_text = "<b>📊 Live Dashboard:</b> No active task running."
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Refresh", callback_data="btn_status")]])

    if isinstance(target_ctx, Message):
        sent_msg = await target_ctx.reply_text(card_text, reply_markup=keyboard, disable_web_page_preview=True)
        if task_running:
            active_dashboard_msg = sent_msg
    elif isinstance(target_ctx, CallbackQuery):
        try:
            await target_ctx.message.edit_text(card_text, reply_markup=keyboard, disable_web_page_preview=True)
        except Exception:
            pass
        await target_ctx.answer("Refreshed")

@app.on_message(ALLOWED_FILTER & filters.command(["settarget"], prefixes=["/", "."]))
async def set_target_cmd(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 2: return
    set_config("target_chat", args[1].strip())
    await message.reply_text(f"✅ Mode 1 Target set to: <code>{args[1].strip()}</code>")

@app.on_message(ALLOWED_FILTER & filters.command(["settarget2"], prefixes=["/", "."]))
async def set_target2_cmd(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 2: return
    set_config("mode2_target", args[1].strip())
    await message.reply_text(f"✅ Mode 2 Target set to: <code>{args[1].strip()}</code>")

@app.on_message(ALLOWED_FILTER & filters.command(["setbrand"], prefixes=["/", "."]))
async def set_brand_cmd(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2: return
    set_config("brand_name", args[1].strip())
    await message.reply_text(f"✅ Mode 1 Brand set to: <code>{args[1].strip()}</code>")

@app.on_message(ALLOWED_FILTER & filters.command(["setbrand2"], prefixes=["/", "."]))
async def set_brand2_cmd(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2: return
    set_config("mode2_brand", args[1].strip())
    await message.reply_text(f"✅ Mode 2 Brand set to: <code>{args[1].strip()}</code>")

@app.on_message(ALLOWED_FILTER & filters.command(["mode2live"], prefixes=["/", "."]))
async def mode2_live_cmd(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 2: return
    val = args[1].strip().lower()
    if val in ["on", "off"]:
        set_config("mode2_live_active", val)
        await message.reply_text(f"✅ Mode 2 Live Auto-Watermark is now: <code>{val.upper()}</code>")

@app.on_message(ALLOWED_FILTER & filters.command(["setpercentage"], prefixes=["/", "."]))
async def set_percentage_cmd(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 2: return
    try:
        pct = int(args[1].strip())
        set_config("brand_percentage", str(pct))
        set_config("mode2_brand_percentage", str(pct))
        await message.reply_text(f"✅ Watermark percentage set to: <code>{pct}%</code>")
    except ValueError:
        pass

@app.on_callback_query()
async def handle_callbacks(client: Client, callback: CallbackQuery):
    global is_paused, task_running, task_cancelled
    data = callback.data
    if data == "btn_status":
        await send_status_view(callback)
    elif data == "btn_pause":
        is_paused = True
        await callback.answer("Paused ⏸️")
        await send_status_view(callback)
    elif data == "btn_resume":
        is_paused = False
        await callback.answer("Resumed ▶️")
        await send_status_view(callback)
    elif data == "btn_stop":
        task_cancelled = True
        task_running = False
        delete_task_progress()
        await callback.answer("Cancelled 🛑")
        await send_status_view(callback)

# ==================== MODE 1: COPY COMMAND ====================
@app.on_message(ALLOWED_FILTER & filters.command(["copy"], prefixes=["/", "."]))
async def start_copy_command(client: Client, message: Message):
    global task_running, is_paused, task_cancelled, task_start_time

    if task_running:
        await message.reply_text("⚠️ Task already running!")
        return

    dest_chat = get_config("target_chat")
    if not dest_chat:
        await message.reply_text("❌ Target channel not set! Use `/settarget`")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.reply_text("❌ Usage: `/copy <link>`")
        return

    source_chat, start_msg_id, end_msg_id = parse_telegram_link(args[1])
    if not source_chat or not start_msg_id:
        await message.reply_text("❌ Invalid link!")
        return

    is_paused = False
    task_cancelled = False
    task_start_time = time.time()
    save_progress(str(source_chat), str(dest_chat), start_msg_id, start_msg_id, end_msg_id, 0, 0, 0, 0, "RUNNING")

    asyncio.create_task(
        run_copy_process(
            client, message, source_chat, dest_chat, start_msg_id, start_msg_id, end_msg_id,
            0, 0, 0, 0, is_mode2=False
        )
    )

# ==================== MODE 2: RANGE IN-PLACE EDITOR COMMAND ====================
@app.on_message(ALLOWED_FILTER & filters.command(["mode2"], prefixes=["/", "."]))
async def start_mode2_command(client: Client, message: Message):
    global task_running, is_paused, task_cancelled, task_start_time

    if task_running:
        await message.reply_text("⚠️ Task already running!")
        return

    dest_chat = get_config("mode2_target")
    if not dest_chat:
        await message.reply_text("❌ Mode 2 Target channel not set! Use `/settarget2 <channel_id>`")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.reply_text(
            "❌ <b>Usage:</b> <code>/mode2 &lt;link&gt; &lt;start&gt;-&lt;end&gt;</code>\n"
            "Example: <code>/mode2 https://t.me/c/12345/1 1-5000</code>"
        )
        return

    link = args[1]
    chat_id, parsed_start, parsed_end = parse_telegram_link(link)
    
    start_msg_id = parsed_start
    end_msg_id = parsed_end

    if len(args) >= 3 and "-" in args[2]:
        try:
            s_part, e_part = args[2].split("-")
            start_msg_id = int(s_part)
            end_msg_id = int(e_part)
        except ValueError:
            pass

    if not chat_id or not start_msg_id or not end_msg_id:
        await message.reply_text("❌ Invalid link or range format!")
        return

    is_paused = False
    task_cancelled = False
    task_start_time = time.time()
    
    save_progress(str(dest_chat), str(dest_chat), start_msg_id, start_msg_id, end_msg_id, 0, 0, 0, 0, "RUNNING")

    asyncio.create_task(
        run_copy_process(
            client, message, dest_chat, dest_chat, start_msg_id, start_msg_id, end_msg_id,
            0, 0, 0, 0, is_mode2=True
        )
    )

# ==================== MODE 2 LIVE LISTENER ====================
@app.on_message(filters.all)
async def mode2_live_listener(client: Client, message: Message):
    if get_config("mode2_live_active", "off").lower() != "on":
        return

    target_chat = get_config("mode2_target")
    if not target_chat:
        return

    try:
        target_id = int(target_chat)
    except ValueError:
        return

    if message.chat and message.chat.id == target_id:
        if message.from_user and message.from_user.is_self:
            return
        if message.service:
            return

        brand = get_config("mode2_brand", "@skillneast1")
        prefix = get_config("mode2_prefix", "Extracted By")
        pct = int(get_config("mode2_brand_percentage", "40"))

        raw_caption = message.caption or message.text or ""
        is_pure_text = bool(message.text and not message.media)

        final_caption, was_branded = process_caption_custom(
            raw_caption,
            brand=brand,
            prefix=prefix,
            enabled="on",
            percentage_val=pct,
            is_pure_text=is_pure_text
        )

        if was_branded:
            try:
                if message.media:
                    if message.caption != final_caption:
                        await client.edit_message_caption(
                            chat_id=target_id,
                            message_id=message.id,
                            caption=final_caption
                        )
                elif message.text:
                    if message.text != final_caption:
                        await client.edit_message_text(
                            chat_id=target_id,
                            message_id=message.id,
                            text=final_caption
                        )
            except Exception:
                pass

# ==================== CORE DUAL-MODE WORKER ====================
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
    initial_texts_count: int,
    initial_branded_count: int,
    is_mode2: bool = False,
):
    global task_running, is_paused, task_cancelled, task_start_time, active_dashboard_msg
    task_running = True
    is_paused = False
    task_cancelled = False

    try:
        dest_chat_obj = await client.get_chat(dest_chat)
        if not is_mode2:
            source_chat_obj = await client.get_chat(source_chat)
        else:
            source_chat_obj = dest_chat_obj
    except Exception as e:
        await notify_message.reply_text(f"❌ Chat Access Error: `{e}`")
        task_running = False
        return

    if is_mode2:
        brand = get_config("mode2_brand", "@skillneast1")
        prefix = get_config("mode2_prefix", "Extracted By")
        enabled = get_config("mode2_brand_enabled", "on")
        pct = int(get_config("mode2_brand_percentage", "40"))
        mode_title = "MODE 2 (IN-PLACE EDITOR)"
    else:
        brand = get_config("brand_name", "@skillneast1")
        prefix = get_config("caption_prefix", "Extracted By")
        enabled = get_config("brand_enabled", "on")
        pct = int(get_config("brand_percentage", "40"))
        mode_title = "MODE 1 (COPY COPIER)"

    current_id = current_start
    copied_count = initial_copied_count
    videos_count = initial_videos_count
    texts_count = initial_texts_count
    branded_count = initial_branded_count

    initial_card, initial_keyboard = render_dashboard(
        mode_title=mode_title,
        source_chat=str(source_chat),
        dest_chat=str(dest_chat),
        brand=brand,
        prefix=prefix,
        start_id=start_id,
        current_id=current_id,
        last_id=last_id,
        copied_count=copied_count,
        videos_count=videos_count,
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
                copied_count, videos_count, texts_count, branded_count, "PAUSED"
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
                
                final_caption, was_branded = process_caption_custom(
                    raw_caption,
                    brand=brand,
                    prefix=prefix,
                    enabled=enabled,
                    percentage_val=pct,
                    is_pure_text=is_pure_text
                )

                if was_branded:
                    branded_count += 1

                try:
                    if is_mode2:
                        if msg.media:
                            videos_count += 1
                            if msg.caption != final_caption:
                                await client.edit_message_caption(
                                    chat_id=dest_chat_obj.id,
                                    message_id=msg.id,
                                    caption=final_caption
                                )
                        elif msg.text:
                            texts_count += 1
                            if msg.text != final_caption:
                                await client.edit_message_text(
                                    chat_id=dest_chat_obj.id,
                                    message_id=msg.id,
                                    text=final_caption
                                )
                        copied_count += 1
                    else:
                        if msg.media:
                            videos_count += 1
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
                    pass

                await asyncio.sleep(DELAY_SECONDS)

            current_id += 1
            save_progress(
                str(source_chat), str(dest_chat), start_id, current_id, last_id,
                copied_count, videos_count, texts_count, branded_count, "RUNNING"
            )

            now = time.time()
            if (now - last_dashboard_edit_time >= 5.0) or (current_id > last_id):
                last_dashboard_edit_time = time.time()
                updated_card, updated_keyboard = render_dashboard(
                    mode_title=mode_title,
                    source_chat=str(source_chat),
                    dest_chat=str(dest_chat),
                    brand=brand,
                    prefix=prefix,
                    start_id=start_id,
                    current_id=min(current_id, last_id),
                    last_id=last_id,
                    copied_count=copied_count,
                    videos_count=videos_count,
                    texts_count=texts_count,
                    branded_count=branded_count,
                    status_label="RUNNING 🟢",
                    start_time=task_start_time,
                )
                try:
                    if active_dashboard_msg:
                        await active_dashboard_msg.edit_text(
                            updated_card, reply_markup=updated_keyboard, disable_web_page_preview=True
                        )
                except Exception:
                    pass

        except FloodWait as e:
            await asyncio.sleep(e.value)
        except Exception:
            current_id += 1

    task_running = False
    delete_task_progress()

    completed_card, completed_keyboard = render_dashboard(
        mode_title=mode_title,
        source_chat=str(source_chat),
        dest_chat=str(dest_chat),
        brand=brand,
        prefix=prefix,
        start_id=start_id,
        current_id=last_id,
        last_id=last_id,
        copied_count=copied_count,
        videos_count=videos_count,
        texts_count=texts_count,
        branded_count=branded_count,
        status_label="COMPLETED 🎉",
        start_time=task_start_time,
    )
    try:
        if active_dashboard_msg:
            active_dashboard_msg.edit_text(completed_card, reply_markup=completed_keyboard, disable_web_page_preview=True)
        else:
            notify_message.reply_text(completed_card, reply_markup=completed_keyboard)
    except Exception:
        notify_message.reply_text(completed_card, reply_markup=completed_keyboard)

# ==================== RUNNER ====================
async def main():
    await app.start()
    print("⚡ Syncing dialogs into peer cache...")
    await sync_dialogs(app)
    print("✅ Ultimate Dual-Mode V4 Userbot is Online & Ready on Railway!")
    await start_web_server()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
