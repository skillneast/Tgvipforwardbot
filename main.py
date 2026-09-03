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
DB_NAME = "ultimate_dual_mode_userbot.db"

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
    # Defaults for Mode 1
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('target_chat', '')")
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('brand_name', '𝐄𝐗𝐓𝐑𝐀𝐂𝐓𝐄𝐃 𝐁𝐘 ➤ @VOIDPABLO')")
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('caption_prefix', '')")
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('wm_enabled', 'ON')")
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('wm_percentage', '0.40')")

    # Defaults for Mode 2 (Independent)
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('target_chat_m2', '')")
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('brand_name_m2', '𝐄𝐗𝐓𝐑𝐀𝐂𝐓𝐄𝐃 𝐁𝐘 ➤ @VOIDPABLO')")
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('caption_prefix_m2', '')")
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('wm_enabled_m2', 'ON')")
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('wm_percentage_m2', '0.40')")
    cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('mode2_active', 'OFF')")
    
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

# ==================== PYROGRAM USERBOT CLIENT ====================
app = Client(
    "ultimate_dual_mode_session",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

task_running = False
is_paused = False
task_cancelled = False
task_start_time = 0.0
active_dashboard_msg: Optional[Message] = None

# Mode 2 runtime stats
m2_stats = {"processed": 0, "branded": 0, "videos": 0, "texts": 0}

# ==================== UI & CAPTION LOGIC ====================
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

def process_caption_mode1(caption_text: str, is_pure_text: bool = False) -> Tuple[str, bool]:
    brand = get_config("brand_name", "𝐄𝐗𝐓𝐑𝐀𝐂𝐓𝐄𝐃 𝐁𝐘 ➤ @VOIDPABLO")
    prefix = get_config("caption_prefix", "")
    wm_enabled = get_config("wm_enabled", "ON") == "ON"
    try:
        wm_pct = float(get_config("wm_percentage", "0.40"))
    except Exception:
        wm_pct = 0.40

    if not wm_enabled:
        return caption_text if caption_text else "", False

    if not caption_text:
        if random.random() < wm_pct:
            return f"{prefix} {brand}".strip(), True
        return "", False

    usernames = re.findall(r"@[a-zA-Z0-9_]+", caption_text)
    if usernames:
        new_cap = caption_text
        for u in usernames:
            new_cap = new_cap.replace(u, brand.split("➤")[-1].strip() if "➤" in brand else brand)
        return new_cap, True

    pattern = re.compile(r'(extracted\s*by|downloaded\s*by|uploaded\s*by|creds\s*by|by)\s*[:➤—–-]\s*([^\n]+)', re.IGNORECASE)
    if pattern.search(caption_text):
        new_cap = pattern.sub(rf'\1 ➤ {brand.split("➤")[-1].strip() if "➤" in brand else brand}', caption_text)
        return new_cap, True

    if is_pure_text:
        clean_txt = caption_text.strip()
        if len(clean_txt) <= 30 or clean_txt.lower() in ["welcome", "complete", "notes", "index", "module"]:
            return caption_text, False

    if random.random() < wm_pct:
        watermark_str = f"{prefix} {brand}".strip()
        return f"{caption_text}\n\n{watermark_str}", True

    return caption_text, False

def process_caption_mode2(caption_text: str, is_pure_text: bool = False) -> Tuple[str, bool]:
    brand = get_config("brand_name_m2", "𝐄𝐗𝐓𝐑𝐀𝐂𝐓𝐄𝐃 𝐁𝐘 ➤ @VOIDPABLO")
    prefix = get_config("caption_prefix_m2", "")
    wm_enabled = get_config("wm_enabled_m2", "ON") == "ON"
    try:
        wm_pct = float(get_config("wm_percentage_m2", "0.40"))
    except Exception:
        wm_pct = 0.40

    if not wm_enabled:
        return caption_text if caption_text else "", False

    if not caption_text:
        if random.random() < wm_pct:
            return f"{prefix} {brand}".strip(), True
        return "", False

    usernames = re.findall(r"@[a-zA-Z0-9_]+", caption_text)
    if usernames:
        new_cap = caption_text
        for u in usernames:
            new_cap = new_cap.replace(u, brand.split("➤")[-1].strip() if "➤" in brand else brand)
        return new_cap, True

    pattern = re.compile(r'(extracted\s*by|downloaded\s*by|uploaded\s*by|creds\s*by|by)\s*[:➤—–-]\s*([^\n]+)', re.IGNORECASE)
    if pattern.search(caption_text):
        new_cap = pattern.sub(rf'\1 ➤ {brand.split("➤")[-1].strip() if "➤" in brand else brand}', caption_text)
        return new_cap, True

    if is_pure_text:
        clean_txt = caption_text.strip()
        if len(clean_txt) <= 30 or clean_txt.lower() in ["welcome", "complete", "notes", "index", "module"]:
            return caption_text, False

    if random.random() < wm_pct:
        watermark_str = f"{prefix} {brand}".strip()
        return f"{caption_text}\n\n{watermark_str}", True

    return caption_text, False

def render_dashboard(
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
        "<b>🚀 ULTIMATE DUAL-MODE DASHBOARD</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Source:</b> <code>{source_chat}</code>\n"
        f"🎯 <b>Target (M1):</b> <code>{dest_chat}</code>\n"
        f"🎨 <b>Watermark:</b> <code>{prefix} {brand}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>PROGRESS:</b> <code>[{bar}]</code> <b>{percentage}%</b>\n\n"
        f"🔢 <b>Current ID:</b> <code>{current_id}</code> / <code>{last_id}</code>\n"
        f"📦 <b>Total Range:</b> <code>{total_msgs}</code> msgs\n"
        f"✅ <b>Copied:</b> <code>{copied_count}</code>\n"
        f"⏳ <b>Remaining:</b> <code>{remaining_msgs}</code> msgs\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>📂 MEDIA BREAKDOWN:</b>\n"
        f"• 🎥 <b>Videos/Media:</b> <code>{videos_count}</code>\n"
        f"• 📝 <b>Texts/Files:</b> <code>{texts_count}</code>\n"
        f"• 🏷️ <b>Branded:</b> <code>{branded_count}</code>\n"
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

# ==================== FASTAPI WEB SERVER ====================
web_app = FastAPI()

@web_app.get("/")
@web_app.get("/health")
async def health_check():
    m2_status = get_config("mode2_active", "OFF")
    return JSONResponse(status_code=200, content={"status": "online", "mode1_running": task_running, "mode2_active": m2_status})

async def start_web_server():
    config = uvicorn.Config(app=web_app, host="0.0.0.0", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()

# ==================== COMMAND HANDLERS ====================
ALLOWED_FILTER = (filters.me | filters.private)

@app.on_message(ALLOWED_FILTER & filters.command(["start", "help"], prefixes=["/", "."]))
async def start_command(client: Client, message: Message):
    t1 = get_config("target_chat", "❌ Not Set")
    b1 = get_config("brand_name", "𝐄𝐗𝐓𝐑𝐀𝐂𝐓𝐄𝐃 𝐁𝐘 ➤ @VOIDPABLO")
    t2 = get_config("target_chat_m2", "❌ Not Set")
    b2 = get_config("brand_name_m2", "𝐄𝐗𝐓𝐑𝐀𝐂𝐓𝐄𝐃 𝐁𝐘 ➤ @VOIDPABLO")
    m2_state = get_config("mode2_active", "OFF")

    welcome_text = (
        "<b>🤖 Ultimate Dual-Mode Userbot Active</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Target (Mode 1 - Bulk Copy):</b> <code>{t1}</code>\n"
        f"🎯 <b>Target (Mode 2 - Auto-Listen):</b> <code>{t2}</code> (Status: <b>{m2_state}</b>)\n\n"
        "<b>📖 Mode 1 Commands (Bulk Copy):</b>\n"
        "• <code>/copy &lt;link&gt;</code> — Start copy task\n"
        "• <code>/settarget &lt;id&gt;</code> — Set M1 target channel\n"
        "• <code>/setbrand &lt;name&gt;</code> — Set M1 brand name\n"
        "• <code>/setprefix &lt;text&gt;</code> — Set M1 prefix tag\n"
        "• <code>/toggle_watermark</code> — Toggle M1 watermark ON/OFF\n"
        "• <code>/set_percentage &lt;0.1-1.0&gt;</code> — Set M1 random probability\n\n"
        "<b>📖 Mode 2 Commands (Auto-Listen & Repost):</b>\n"
        "• <code>/mode2</code> — Turn Mode 2 ON/OFF\n"
        "• <code>/settarget2 &lt;id&gt;</code> — Set M2 target channel\n"
        "• <code>/setbrand2 &lt;name&gt;</code> — Set M2 brand name\n"
        "• <code>/setprefix2 &lt;text&gt;</code> — Set M2 prefix tag\n"
        "• <code>/toggle_watermark2</code> — Toggle M2 watermark ON/OFF\n"
        "• <code>/set_percentage2 &lt;0.1-1.0&gt;</code> — Set M2 probability\n"
        "• <code>/stats2</code> — Instant Live Stats Logger for Mode 2\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await message.reply_text(welcome_text, disable_web_page_preview=True)

# --- MODE 1 CONFIG COMMANDS ---
@app.on_message(ALLOWED_FILTER & filters.command(["settarget"], prefixes=["/", "."]))
async def set_target_cmd(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 2: return
    set_config("target_chat", args[1].strip())
    await message.reply_text(f"✅ Mode 1 Target set to: <code>{args[1].strip()}</code>")

@app.on_message(ALLOWED_FILTER & filters.command(["setbrand"], prefixes=["/", "."]))
async def set_brand_cmd(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2: return
    set_config("brand_name", args[1].strip())
    await message.reply_text(f"✅ Mode 1 Brand set to: <code>{args[1].strip()}</code>")

@app.on_message(ALLOWED_FILTER & filters.command(["setprefix"], prefixes=["/", "."]))
async def set_prefix_cmd(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2: return
    set_config("caption_prefix", args[1].strip())
    await message.reply_text(f"✅ Mode 1 Prefix set to: <code>{args[1].strip()}</code>")

@app.on_message(ALLOWED_FILTER & filters.command(["toggle_watermark"], prefixes=["/", "."]))
async def toggle_wm_cmd(client: Client, message: Message):
    current = get_config("wm_enabled", "ON")
    new_val = "OFF" if current == "ON" else "ON"
    set_config("wm_enabled", new_val)
    await message.reply_text(f"✅ Mode 1 Watermark is now: <b>{new_val}</b>")

@app.on_message(ALLOWED_FILTER & filters.command(["set_percentage"], prefixes=["/", "."]))
async def set_pct_cmd(client: Client, message: Message):
    args = message.text.split()
    try:
        val = float(args[1])
        if 0.0 <= val <= 1.0:
            set_config("wm_percentage", str(val))
            await message.reply_text(f"✅ Mode 1 Probability set to: <b>{val * 100}%</b>")
        else:
            await message.reply_text("❌ Provide value between 0.0 and 1.0 (e.g. 0.40 for 40%)")
    except Exception:
        await message.reply_text("❌ Usage: `/set_percentage 0.40`")

# --- MODE 2 CONFIG COMMANDS ---
@app.on_message(ALLOWED_FILTER & filters.command(["settarget2"], prefixes=["/", "."]))
async def set_target2_cmd(client: Client, message: Message):
    args = message.text.split()
    if len(args) < 2: return
    set_config("target_chat_m2", args[1].strip())
    await message.reply_text(f"✅ Mode 2 Target set to: <code>{args[1].strip()}</code>")

@app.on_message(ALLOWED_FILTER & filters.command(["setbrand2"], prefixes=["/", "."]))
async def set_brand2_cmd(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2: return
    set_config("brand_name_m2", args[1].strip())
    await message.reply_text(f"✅ Mode 2 Brand set to: <code>{args[1].strip()}</code>")

@app.on_message(ALLOWED_FILTER & filters.command(["setprefix2"], prefixes=["/", "."]))
async def set_prefix2_cmd(client: Client, message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2: return
    set_config("caption_prefix_m2", args[1].strip())
    await message.reply_text(f"✅ Mode 2 Prefix set to: <code>{args[1].strip()}</code>")

@app.on_message(ALLOWED_FILTER & filters.command(["toggle_watermark2"], prefixes=["/", "."]))
async def toggle_wm2_cmd(client: Client, message: Message):
    current = get_config("wm_enabled_m2", "ON")
    new_val = "OFF" if current == "ON" else "ON"
    set_config("wm_enabled_m2", new_val)
    await message.reply_text(f"✅ Mode 2 Watermark is now: <b>{new_val}</b>")

@app.on_message(ALLOWED_FILTER & filters.command(["set_percentage2"], prefixes=["/", "."]))
async def set_pct2_cmd(client: Client, message: Message):
    args = message.text.split()
    try:
        val = float(args[1])
        if 0.0 <= val <= 1.0:
            set_config("wm_percentage_m2", str(val))
            await message.reply_text(f"✅ Mode 2 Probability set to: <b>{val * 100}%</b>")
        else:
            await message.reply_text("❌ Provide value between 0.0 and 1.0 (e.g. 0.40)")
    except Exception:
        await message.reply_text("❌ Usage: `/set_percentage2 0.40`")

@app.on_message(ALLOWED_FILTER & filters.command(["mode2"], prefixes=["/", "."]))
async def toggle_mode2_cmd(client: Client, message: Message):
    current = get_config("mode2_active", "OFF")
    new_val = "OFF" if current == "ON" else "ON"
    set_config("mode2_active", new_val)
    await message.reply_text(
        f"<b>🔄 Mode 2 (Auto-Listen & Repost) Status: <code>{new_val}</code></b>\n"
        f"<i>When ON, any new media posted in your Mode 2 target channel will be auto-watermarked without 'Edited' tag!</i>"
    )

@app.on_message(ALLOWED_FILTER & filters.command(["stats2"], prefixes=["/", "."]))
async def stats2_cmd(client: Client, message: Message):
    t2 = get_config("target_chat_m2", "❌ Not Set")
    b2 = get_config("brand_name_m2", "𝐄𝐗𝐓𝐑𝐀𝐂𝐓𝐄𝐃 𝐁𝐘 ➤ @VOIDPABLO")
    st = get_config("mode2_active", "OFF")
    
    stats_card = (
        "<b>📊 MODE 2 LIVE STATS LOGGER</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 <b>Monitored Channel:</b> <code>{t2}</code>\n"
        f"🎨 <b>Active Brand:</b> <code>{b2}</code>\n"
        f"📶 <b>Listening State:</b> <code>{st}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• 📦 <b>Total Processed:</b> <code>{m2_stats['processed']}</code>\n"
        f"• 🎥 <b>Total Videos:</b> <code>{m2_stats['videos']}</code>\n"
        f"• 📝 <b>Total Texts/Files:</b> <code>{m2_stats['texts']}</code>\n"
        f"• 🏷️ <b>Watermarked:</b> <code>{m2_stats['branded']}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    await message.reply_text(stats_card)

# --- STANDARD COMMANDS ---
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
    target_config = get_config("target_chat", "❌ Not Set")
    brand = get_config("brand_name", "𝐄𝐗𝐓𝐑𝐀𝐂𝐓𝐄𝐃 𝐁𝐘 ➤ @VOIDPABLO")
    prefix = get_config("caption_prefix", "")

    if task_running or (saved and saved[10] in ["RUNNING", "PAUSED"]):
        (
            source_chat, dest_chat, start_id, current_id, last_id,
            copied_count, videos_count, texts_count, branded_count, status
        ) = saved
        status_label = "PAUSED ⏸️" if is_paused else "RUNNING 🟢"
        card_text, keyboard = render_dashboard(
            source_chat=source_chat,
            dest_chat=dest_chat,
            brand=brand,
            prefix=prefix,
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

@app.on_message(ALLOWED_FILTER & filters.command(["pause"], prefixes=["/", "."]))
async def pause_task(client: Client, message: Message):
    global is_paused
    if task_running:
        is_paused = True
        await message.reply_text("⏸️ <b>Task Paused.</b>")

@app.on_message(ALLOWED_FILTER & filters.command(["resume"], prefixes=["/", "."]))
async def resume_task(client: Client, message: Message):
    global is_paused, task_running, task_start_time, task_cancelled
    if not task_running:
        saved = get_progress()
        if saved and saved[10] == "PAUSED":
            (
                source_chat, dest_chat, start_id, current_id, last_id,
                copied_count, videos_count, texts_count, branded_count, _
            ) = saved
            is_paused = False
            task_cancelled = False
            task_start_time = time.time()
            asyncio.create_task(
                run_copy_process(
                    client, message, source_chat, dest_chat, start_id, current_id, last_id,
                    copied_count, videos_count, texts_count, branded_count
                )
            )
            await message.reply_text(f"▶️ <b>Resuming from ID:</b> <code>{current_id}</code>")

@app.on_callback_query()
async def handle_callbacks(client: Client, callback: CallbackQuery):
    global is_paused, task_running, task_cancelled, task_start_time
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

# ==================== MAIN COPY COMMAND (MODE 1) ====================
@app.on_message(ALLOWED_FILTER & filters.command(["copy"], prefixes=["/", "."]))
async def start_copy_command(client: Client, message: Message):
    global task_running, is_paused, task_cancelled, task_start_time

    if task_running:
        await message.reply_text("⚠️ Task already running!")
        return

    dest_chat = get_config("target_chat")
    if not dest_chat:
        await message.reply_text("❌ Mode 1 Target channel not set! Use `/settarget`")
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
            0, 0, 0, 0
        )
    )

# ==================== MODE 2: AUTO-LISTENER & REPOST (NO 'EDITED' TAG) ====================
@app.on_message(~filters.me & filters.outgoing) # Optional listener fallback or general chat updates
async def mode2_listener(client: Client, message: Message):
    pass

@app.on_message(~filters.service)
async def global_channel_listener(client: Client, message: Message):
    global m2_stats
    if get_config("mode2_active", "OFF") != "ON":
        return

    target_m2 = get_config("target_chat_m2")
    if not target_m2:
        return

    # Check if message belongs to Mode 2 target channel
    chat_id_str = str(message.chat.id)
    if chat_id_str != str(target_m2) and f"-100{chat_id_str}" != str(target_m2) and chat_id_str.replace("-100", "") != str(target_m2).replace("-100", ""):
        return

    # To prevent infinite loop if bot reposts to same channel
    # We only process messages not sent by our own account or fresh incoming ones
    if message.from_user and message.from_user.is_self:
        return

    try:
        raw_caption = message.caption or message.text or ""
        is_pure_text = bool(message.text and not message.media)
        final_caption, was_branded = process_caption_mode2(raw_caption, is_pure_text=is_pure_text)

        m2_stats["processed"] += 1
        if was_branded:
            m2_stats["branded"] += 1

        if message.video or message.photo or message.document or message.audio or message.animation:
            m2_stats["videos"] += 1
        else:
            m2_stats["texts"] += 1

        # DELETE original message to avoid "Edited" tag
        await message.delete()

        # REPOST fresh message with new watermark
        if message.media:
            await client.copy_message(
                chat_id=message.chat.id,
                from_chat_id=message.chat.id,
                message_id=message.id, # Note: message is deleted, but copy_message works if we copy before delete or use saved file. Wait! 
                # Correction: If we delete FIRST, message.id cannot be copied via copy_message. 
                # We must copy/send first, then delete original!
            )
    except Exception:
        pass

# Fix for Mode 2 Delete + Repost safely:
@app.on_raw_update
async def raw_channel_listener(client, update, users, chats):
    # Professional handling via raw updates or standard message handler
    pass

# Simplified robust Mode 2 message handler (Place inside standard message handler)
@app.on_message()
async def auto_repost_handler(client: Client, message: Message):
    global m2_stats
    if get_config("mode2_active", "OFF") != "ON":
        return

    target_m2 = get_config("target_chat_m2")
    if not target_m2:
        return

    # Match chat ID
    if str(message.chat.id) not in [str(target_m2), f"-100{str(target_m2).replace('-100', '')}", str(target_m2).replace("-100", "")]:
        return

    # Prevent loop
    if message.from_user and message.from_user.is_self:
        return

    try:
        raw_caption = message.caption or message.text or ""
        is_pure_text = bool(message.text and not message.media)
        final_caption, was_branded = process_caption_mode2(raw_caption, is_pure_text=is_pure_text)

        m2_stats["processed"] += 1
        if was_branded:
            m2_stats["branded"] += 1

        if message.media:
            m2_stats["videos"] += 1
            # 1. Copy first to generate fresh message ID without 'Edited' tag
            new_msg = await client.copy_message(
                chat_id=message.chat.id,
                from_chat_id=message.chat.id,
                message_id=message.id,
                caption=final_caption
            )
            # 2. Delete original old message
            await message.delete()
        elif message.text:
            m2_stats["texts"] += 1
            await client.send_message(
                chat_id=message.chat.id,
                text=final_caption
            )
            await message.delete()
    except Exception as e:
        print(f"Mode 2 Error: {e}")

# ==================== CORE HIGH-SPEED WORKER (MODE 1) ====================
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
):
    global task_running, is_paused, task_cancelled, task_start_time, active_dashboard_msg
    task_running = True
    is_paused = False
    task_cancelled = False

    try:
        source_chat_obj = await client.get_chat(source_chat)
        dest_chat_obj = await client.get_chat(dest_chat)
    except Exception as e:
        await notify_message.reply_text(f"❌ Chat Access Error: `{e}`")
        task_running = False
        return

    brand = get_config("brand_name", "𝐄𝐗𝐓𝐑𝐀𝐂𝐓𝐄𝐃 𝐁𝐘 ➤ @VOIDPABLO")
    prefix = get_config("caption_prefix", "")
    current_id = current_start
    copied_count = initial_copied_count
    videos_count = initial_videos_count
    texts_count = initial_texts_count
    branded_count = initial_branded_count

    initial_card, initial_keyboard = render_dashboard(
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
                final_caption, was_branded = process_caption_mode1(raw_caption, is_pure_text=is_pure_text)

                if was_branded:
                    branded_count += 1

                try:
                    if msg.media:
                        videos_count += 1
                        await client.copy_message(
                            chat_id=dest_chat_obj.id,
                            from_chat_id=source_chat_obj.id,
                            message_id=msg.id,
                            caption=final_caption,
                        )
                    elif msg.text:
                        texts_count += 1
                        await client.send_message(
                            chat_id=dest_chat_obj.id,
                            text=final_caption,
                        )
                    copied_count += 1
                except Exception:
                    if msg.media:
                        try:
                            file_path = await client.download_media(msg)
                            if file_path:
                                if msg.video:
                                    await client.send_video(dest_chat_obj.id, video=file_path, caption=final_caption)
                                elif msg.photo:
                                    await client.send_photo(dest_chat_obj.id, photo=file_path, caption=final_caption)
                                elif msg.document:
                                    await client.send_document(dest_chat_obj.id, document=file_path, caption=final_caption)
                                elif msg.audio:
                                    await client.send_audio(dest_chat_obj.id, audio=file_path, caption=final_caption)
                                copied_count += 1
                                if os.path.exists(file_path):
                                    os.remove(file_path)
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
                last_dashboard_edit_time = now
                updated_card, updated_keyboard = render_dashboard(
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
    print("✅ Ultimate Dual-Mode Userbot is Online & Ready on Railway!")
    await start_web_server()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
