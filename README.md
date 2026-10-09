# Universal Media Downloader — Telegram Bot

A production-ready Telegram bot that downloads **videos, audio and photos** from YouTube, Instagram (posts, **photos**, **carousels**, Reels), TikTok, Twitter/X and 1000+ other sites using yt-dlp.

---

## What changed in this version

| Area | Before | Now |
|---|---|---|
| Instagram **photos / carousels** | ❌ "There is no video in this post" | ✅ Detected and downloaded, sent as a photo album |
| Post detection | Always offered Video/Audio | ✅ Bot inspects the link first and only offers what exists |
| PO Token provider | Script/server version could mismatch → provider silently rejected | ✅ Startup health check; version pinned to `2.0.2` |
| PO Token provider crash | ❌ Aborted the whole download | ✅ Caught, provider switched off, download continues |
| Player-client retries | 1 fixed list | ✅ PO-aware tiers (web family with a token, defaults/android without) |
| Titles with `&` or `<` | ❌ Telegram rejected the message, looked like a failed download | ✅ All HTML is escaped |
| Quality buttons | Always said 1080p but fell back to 360p `best` | ✅ `bv*[height<=Q]+ba` with graceful degradation |
| `ignoreerrors` | ❌ Real error replaced by "yt-dlp returned no info" | ✅ Errors surfaced verbatim |
| Link parsing | Whole message had to be a bare URL | ✅ URL is extracted from any surrounding text |
| Uploading | 120s timeout, no retry | ✅ 600s timeout, document fallback |
| Health | Logs only | ✅ HTTP `/health` endpoint for Railway |

---

## Features (Compared to Original)

| Feature | Original Code | Improved Version |
|---|---|---|
| HTML escaping | ❌ Broken (`&lt;` `&gt;` everywhere) | ✅ Clean, valid HTML |
| Environment variables | ❌ Token hardcoded | ✅ `.env` file |
| Proxy support | ❌ None | ✅ HTTP + SOCKS5 (separate for Telegram & yt-dlp) |
| Cookie support | ❌ None | ✅ Netscape cookies.txt |
| File size check | ❌ None | ✅ Warns/aborts if > limit |
| Progress bar | ❌ None | ✅ Real-time live progress in chat |
| URL validation | ❌ Any text accepted | ✅ Regex validation |
| Error handling | ❌ Fragile | ✅ Per-error-type handling + cleanup |
| Rate limiting | ❌ None | ✅ Configurable per-user throttle |
| Logging | ❌ `print()` only | ✅ File + console, leveled |
| Memory management | ❌ Leaked forever | ✅ Auto-cleanup stale states |
| Thread safety | ❌ None | ✅ Locks on shared state |
| Quality options | ❌ 3 video / 1 audio | ✅ 5 video (480p-4K) / 4 audio (128-320kbps) |
| File cleanup | ❌ Only on success | ✅ Always (finally block + orphan scan) |
| Info/help system | ❌ None | ✅ Interactive menus + /status command |
| 2026 YouTube support | ❌ Fails on SABR | ✅ PO token + multi-client fallback |
| User-agent spoofing | ❌ Default | ✅ Real per-client UA |
| Polling resilience | ❌ Crashes on error | ✅ Auto-restart with delay |
| Instagram photos | ❌ Unsupported | ✅ Single photos and carousels (album) |
| Telegram HTML | ❌ Titles broke the message | ✅ Escaped everywhere |

---

## Quick Start

### 1. Prerequisites

```bash
# Install system dependencies
# Linux (Debian/Ubuntu):
sudo apt update
sudo apt install ffmpeg python3 python3-pip python3-venv -y

# macOS:
brew install ffmpeg python3

# Windows:
# Download ffmpeg from https://ffmpeg.org/download.html
# Install Python from https://python.org
```

### 2. Clone & Setup

```bash
cd ~/downloader_bot

# Create virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate  # Linux/macOS
# venv\Scripts\activate   # Windows

# Install Python dependencies
pip install -r requirements.txt
```

### 3. Configure Environment

```bash
# Copy example env file
cp .env.example .env

# Edit with your values
nano .env
```

**Minimum required:**
```
BOT_TOKEN=123456:ABC-DEF1234ghijk
```

**With proxy (Iran users):**
```
BOT_TOKEN=123456:ABC-DEF1234ghijk
PROXY_HTTP=socks5://127.0.0.1:1080
PROXY_HTTPS=socks5://127.0.0.1:1080
YTDLP_PROXY=socks5://127.0.0.1:1080
```

**Proxy formats supported:**
- `socks5://127.0.0.1:1080` (SOCKS5, e.g., v2rayN, Shadowsocks)
- `socks5://user:pass@host:port` (Authenticated SOCKS5)
- `http://127.0.0.1:8080` (HTTP proxy)
- `https://user:pass@host:8080` (HTTPS proxy)

### 4. Get Your Bot Token

1. Open Telegram, search for `@BotFather`
2. Send `/newbot`
3. Follow the prompts to create a bot
4. Copy the token (looks like `1234567890:AAFfjks...`)
5. Paste it into your `.env` file

### 5. (Recommended) Set Up Cookies

For Instagram, age-restricted YouTube, and private content:

**Method A — Browser Extension (easiest):**
1. Install "Get cookies.txt LOCALLY" extension (Chrome/Edge/Firefox)
2. Log in to Instagram / YouTube in your browser
3. Click the extension → Export cookies.txt
4. Place `cookies.txt` in the bot directory

**Method B — yt-dlp command:**
```bash
yt-dlp --cookies-from-browser chrome --cookies cookies.txt
```

### 6. Run the Bot

```bash
# Make sure venv is active
source venv/bin/activate

# Run
python bot.py
```

For production, use a process manager:

```bash
# Using systemd (Linux):
sudo nano /etc/systemd/system/media-downloader-bot.service

# Using tmux/screen:
tmux new -s bot
python bot.py
# Ctrl+B, D to detach

# Using nohup:
nohup python bot.py > bot.out 2>&1 &
```

---

## Usage

1. **Start chat**: Send `/start` to the bot
2. **Send a link**: Paste any URL (YouTube, Instagram, TikTok, etc.). Extra text around the link is fine.
3. **Choose format**: Tap "Video", "Audio (MP3)" or **"Photos"** (photos only appear when the post actually contains photos)
4. **Choose quality**: Select resolution/bitrate (photos skip this step)
5. **Wait**: Progress bar shows real-time status
6. **Receive file**: Bot sends the video, MP3 or photo album

For an Instagram **carousel** the bot reports how many photos/videos the post has, then sends every photo as a Telegram album (groups of 10).

**Commands:**
- `/start` — Welcome message with interactive menu
- `/status` — Bot health check (sessions, cookies, ffmpeg, proxy)

---

## Supported Sites (1800+)

| Platform | Public | Private/Age-restricted |
|---|---|---|
|| YouTube | ✅ | ✅ (with cookies + PO token) |
|| Instagram Reels | ✅ | ✅ (with cookies) |
|| Instagram Posts | ✅ | ✅ (with cookies) |
|| Instagram **Photos** | ✅ | ✅ (with cookies) |
|| Instagram **Carousels** | ✅ | ✅ (with cookies) |
| TikTok | ✅ | N/A |
| Twitter/X | ✅ | N/A |
| Facebook | ✅ | ✅ (with cookies) |
| Vimeo | ✅ | N/A |
| Reddit | ✅ | N/A |
| Twitch | ✅ | N/A |

---

## Deploying on Railway (Docker)

The repo ships a `Dockerfile` + `start.sh` that run **both** the PO Token
server and the bot in one container.

### Railway environment variables

| Variable | Required | Notes |
|---|---|---|
| `BOT_TOKEN` | ✅ | From `@BotFather` |
| `YOUTUBE_COOKIES` | ✅ for Railway | Full `cookies.txt` content. Datacenter IPs trigger bot checks; cookies are the real fix, the PO token only helps. |
| `YOUTUBE_COOKIES_B64_1..N` | alternative | Same file, gzipped + base64 and split, for hosts that cap one variable at 1024 characters (Back4App). |
| `YOUTUBE_COOKIES_URL` | alternative | URL that serves the `cookies.txt` file; fetched once at startup. |
| `YTDLP_PROXY` | optional | Proxy for downloads, e.g. `http://user:pass@host:port` |
| `MAX_FILE_SIZE_MB` | optional | Telegram hard limit for bots is 50 MB |

`start.sh` exports `PO_TOKEN_SERVER_URL=http://127.0.0.1:4416` for the bot and
binds the PO Token server to **localhost only** (it is unauthenticated).

### Back4App (and any host that caps a variable at 1024 characters)

Back4App rejects an environment variable longer than **1024 characters**, so the
raw `YOUTUBE_COOKIES` value never fits. Generate split variables instead:

```bash
python make_cookie_env.py cookies.txt
```

It gzips the file, base64-encodes it and prints one line per variable — every
value is 1000 characters or less:

```
# cookies.txt : 4486 bytes
# encoded     : 2731 characters -> 3 variable(s) of <= 1000

YOUTUBE_COOKIES_B64_1=eJztvVuPm8j2Rf...
YOUTUBE_COOKIES_B64_2=...
YOUTUBE_COOKIES_B64_3=...
```

Paste **all** of them next to `BOT_TOKEN`. At startup the bot joins the chunks,
gunzips them, writes a temp cookie file and reports it in the log:

```
YouTube cookies loaded from YOUTUBE_COOKIES_B64_* -> /tmp/yt_cookies_ab12cd.txt (23 rows)
```

| Flag | Effect |
|---|---|
| `--youtube-only` | Drop rows that are not YouTube auth cookies — fewer chunks |
| `--chunk 900` | Smaller pieces if the host counts differently |
| `--out cookies.env` | Also write the lines to a file |

**Alternative — one short variable.** Host `cookies.txt` anywhere with an
unguessable URL (a *secret* GitHub Gist works) and set:

```
YOUTUBE_COOKIES_URL=https://gist.githubusercontent.com/you/abc123/raw/cookies.txt
```

Priority at startup: `YOUTUBE_COOKIES_URL` → `YOUTUBE_COOKIES_B64_1..N` →
`YOUTUBE_COOKIES`. A source that fails is skipped with a warning and the next
one is tried.

### Keep the container awake (Back4App free plan)

Back4App's free plan sleeps the container after about **5 minutes with no
inbound traffic**. The bot only makes *outbound* requests while polling, so
they never count and the app goes quiet. Two independent fixes:

#### 1. Uptime monitor — required, this is what actually keeps it up

Point a free monitor at the app's public URL every **3 to 5 minutes**:

| Service | Free tier | Minimum interval |
|---|---|---|
| [UptimeRobot](https://uptimerobot.com/) | 50 monitors | 5 min |
| [cron-job.org](https://cron-job.org/) | 12 jobs | 1 min |

Both only send `GET`, and every one of these answers `200`:

```
/          {"status":"ok","yt_dlp":"...","bgutil_plugin":"2.0.2","po_provider":"http", ...}
/health    same
/healthz   same
```

A monitor that also matches a keyword can look for `"status":"ok"`.
**No code change is needed** — the health server has always been there.

> Polling alone does not keep the container awake. The monitor is the fix.

#### 2. Telegram webhook — optional

Normally the bot *pulls* updates (`getUpdates`). With a webhook, Telegram
*pushes* them into the same HTTP server, so the first message after idle
arrives without waiting for a poll.

Set one extra variable in Back4App:

```
WEBHOOK_URL=https://your-app.b4a.run
```

What the bot does with it:

1. Registers `<WEBHOOK_URL>/telegram` with Telegram, using a random
   `secret_token` (`TELEGRAM_SECRET_TOKEN` overrides it if you want a fixed
   one). Telegram must echo that value in the
   `X-Telegram-Bot-Api-Secret-Token` header, and the server compares it in
   constant time — anything else gets `403`.
2. Serves `POST /telegram` **on the same `$PORT`** as the health endpoint.
   No Flask, no second server, no extra dependency.
3. Stops polling (running both at once makes Telegram answer `409`).
4. If registration fails — bad URL, TLS problem, no listener — it logs the
   error and **falls back to polling on its own**, so a wrong value cannot
   brick the bot.

Leaving `WEBHOOK_URL` unset keeps the safe polling default.

> A webhook does **not** replace the uptime monitor. Between messages there
> is still no inbound traffic, so the container would still sleep.

#### Debugging

| Symptom | Meaning |
|---|---|
| `Webhook registered: https://.../telegram` in Running Logs | Registration worked |
| `set_webhook(...) failed: ...` then `falling back to polling` | URL/TLS problem — fix `WEBHOOK_URL` or delete it |
| `Webhook payload rejected: ...` | Telegram's body could not be parsed; it is acknowledged, not retried |
| `403` on `/telegram` | Header secret mismatch (container restarted with a new generated secret — Telegram picks it up on the next start) |
| `POST /telegram` returns `404` | The health server never bound `$PORT` |

### ⚠️ Version pinning (this was the main bug)

`requirements.txt` pins `bgutil-ytdlp-pot-provider==2.0.2` and the Dockerfile
clones the **same tag**. The provider compares major versions and, on a
mismatch, rejects every request:

```
Plugin and HTTP server major versions are mismatched.
```

The bot then runs without a PO token, and YouTube fails with
"Sign in to confirm you're not a bot" on datacenter IPs.

If you bump one, bump the other. Check it at runtime:

```bash
curl http://127.0.0.1:8080/health
# {"status":"ok","bgutil_plugin":"2.0.2","po_provider":"http","po_detail":"HTTP server 2.0.2 ..."}
```

`/status` in the chat shows the same information.

---

## Troubleshooting

### "Sign in to confirm you're not a bot" (YouTube)

YouTube wants a login from the server's IP. Read `/status` first — the cookie
line now says exactly what is wrong:

| `/status` cookie line | Meaning | Fix |
|---|---|---|
| `Not loaded \| file MISSING` | No cookie variable is set | Set `YOUTUBE_COOKIES_B64_1..N` (see Back4App section) |
| `... rows, N EXPIRED` | The session ran out | Export a fresh `cookies.txt`, regenerate, redeploy |
| `... rows, valid to ...` | Cookies look fine | Export fresh anyway - YouTube rotates sessions, then redeploy |

Also check `PO token provider: off` - if it is off, fix the PO token server (below).

→ Update yt-dlp: `pip install --upgrade yt-dlp`

### `PO token provider: off` with "version mismatch"
→ The pip plugin and the cloned server differ. Pin both to the same version
  (`requirements.txt` ↔ `ARG BGUTIL_TAG` in the `Dockerfile`), then redeploy.

### `PO token provider: off` with "cannot reach"
→ `start.sh` could not bring the server up. Read the deploy logs for the
  `PO Token server exited early` block. The bot still works without it - it
  just falls back to player clients that do not need a token.

### Instagram returns "Login required"
→ You need cookies.txt from a logged-in browser session

### Proxy not working
→ Verify proxy syntax in `.env`
→ For SOCKS5: ensure `pip install requests[socks]` was run
→ Test proxy separately: `curl --socks5 127.0.0.1:1080 https://api.telegram.org`

### File too large (>50MB)
→ Choose lower quality (720p or audio)
→ Or increase MAX_FILE_SIZE_MB in .env (hard limit: 50MB for bots)

### "ffmpeg not found" warning
→ Audio extraction won't work
→ Install ffmpeg: `sudo apt install ffmpeg`

---

## Project Structure

```
downloader_bot/
├── bot.py                 # Main bot code (Instagram photo patch + everything else)
├── requirements.txt       # Python dependencies (bgutil pinned to the Dockerfile tag)
├── Dockerfile             # Railway image: ffmpeg + Deno + PO Token server 2.0.2
├── start.sh               # Starts the PO Token server, then the bot
├── .dockerignore
├── .gitignore
├── .env.example           # Environment template (placeholders only)
├── .env                   # Your configuration (git-ignored)
├── cookies.txt            # Browser cookies (git-ignored)
├── bot.log                # Runtime logs (git-ignored)
└── downloads/             # Temp download directory (auto-cleaned)

# The bot serves GET / and GET /health on $PORT (default 8080).
```

### Rebuilding locally

```bash
docker build -t media-downloader-bot .
docker run --rm --env-file .env -p 127.0.0.1:8080:8080 media-downloader-bot
curl http://127.0.0.1:8080/health
```

---

## Security Notes

- Never commit `.env` or `cookies.txt` to git (`.gitignore` covers both; `.env.example` holds placeholders only)
- Rotate your bot token if it was ever committed - `.env.example` used to contain the real token
- The PO Token server is unauthenticated, so `start.sh` binds it to `127.0.0.1` and the Dockerfile does not publish its port
- Rotate your browser cookies if leaked
- Consider using a firewall to restrict access to the proxy port
- The bot deletes all downloaded files immediately after sending

---

## License

MIT — use freely, attribution appreciated.
