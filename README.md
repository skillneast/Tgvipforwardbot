# Tgvipforwardbot
# ⚡ Telegram Channel Message Forwarder & Re-Poster Userbot

An asynchronous, production-grade Telegram Userbot built on Pyrogram to copy and migrate large message archives between private or public channels with dynamic brand replacement, conditional thumbnail insertion, progress tracking, and 24/7 web-service keep-alive for Render.com.

---

## 🌟 Features

- **Link & Range Copying**: Supports single links (`https://t.me/c/xxxx/100`) and message ranges (`https://t.me/c/xxxx/100-500`).
- **Smart Conditional Thumbnails**:
  - Automatically verifies if an incoming video message already possesses an original thumbnail.
  - If a thumbnail exists $\rightarrow$ replaces it with the user's custom thumbnail saved via `/setthumb`.
  - If no thumbnail exists $\rightarrow$ copies the video without attaching any thumbnail.
- **Dynamic Brand Management**: Change the channel watermarking/caption tag in real-time via `/setbrand <brand_name>` without restarting the bot.
- **Visual Progress Bar UI**: Real-time ASCII progress bar in `/status` along with inline interactive buttons (`Pause`, `Resume`, `Refresh`).
- **Protected Content / Fallback Handling**: If forward restrictions or direct copy fails, the userbot automatically downloads and re-uploads media.
- **Anti-Flood & Rate Limiting**: Built-in 3-second delay and proactive `FloodWait` sleep-retry handling.
- **24/7 Render Keep-Alive**: Includes a background FastAPI server on `/health` for UptimeRobot monitoring.

---

## 📋 Available Commands (Send in Saved Messages)

| Command | Usage / Description |
| :--- | :--- |
| `/start` | Displays the userbot status, active configuration, and quick actions. |
| `/settarget <id>` | Configures the target channel ID (e.g., `/settarget -1004415448802`). |
| `/copy <link>` | Starts copying single/range messages (e.g., `/copy https://t.me/c/123/10-50`). |
| `/setbrand <name>` | Sets the dynamic brand tag (e.g., `/setbrand @MyBrand` or `/setbrand Channel Name`). |
| `/getbrand` | Displays current active brand watermark. |
| `/setthumb` | Reply to any photo with `/setthumb` to save it as the custom replacement thumb. |
| `/status` | Renders a visual status dashboard with progress bar & inline buttons. |
| `/pause` | Temporarily halts message processing. |
| `/resume` | Resumes a paused or stopped task from its saved database checkpoint. |
| `/sync` | Re-syncs joined dialogs to eliminate `PeerIdInvalid` errors. |

---

## 🛠️ Prerequisites

1. **Telegram API ID & Hash**:
   - Go to [my.telegram.org](https://my.telegram.org) $\rightarrow$ Log in $\rightarrow$ **API development tools** $\rightarrow$ Create App $\rightarrow$ Note `api_id` and `api_hash`.
2. **Pyrogram Session String**:
   - Generate via any Telegram session generator bot (like `@StringSessionGeneratorBot` with Pyrogram selected) or with a local Pyrogram script using `Client.export_session_string()`.

---

## 💻 Local Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/your-username/telegram-userbot-copier.git
   cd telegram-userbot-copier
