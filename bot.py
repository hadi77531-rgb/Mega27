#!/usr/bin/env python3
"""
============================================================
  Universal Media Downloader - Telegram Bot
============================================================
Supports : YouTube, Instagram (video + PHOTO + CAROUSEL),
           TikTok, Twitter/X, Facebook and 1000+ sites
Engine   : yt-dlp + pyTelegramBotAPI + bgutil PO Token
Deploy   : Railway (Docker) or local
============================================================
"""

import os
import re
import sys
import html
import json
import time
import gzip
import base64
import shutil
import logging
import subprocess
import tempfile
import threading
import urllib.request
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

import telebot
from telebot import apihelper
from telebot.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
    InputMediaVideo,
)
from dotenv import load_dotenv

import yt_dlp

# ============================================================
#  INSTAGRAM PHOTO SUPPORT
#  yt-dlp's Instagram extractor only handles VIDEO.  A photo
#  post raises "There is no video in this post" and every item
#  of a carousel raises "No video formats found!".  We expose
#  image_versions2.candidates[] as a normal yt-dlp format so
#  photos and carousels download exactly like videos do.
# ============================================================

IG_PATCH_OK = False
try:
    from yt_dlp.extractor.instagram import InstagramIE

    _ig_orig_extract_product_media = InstagramIE._extract_product_media

    def _ig_candidate_score(url: str) -> int:
        """Instagram CDN candidate URL -> rough pixel area.
        The plain, uncropped URL is the original file and always wins."""
        if not url:
            return -1
        if re.search(r"_[sc]\d+x\d+_", url) is None and re.search(r"c\d+\.\d+", url) is None:
            return 10 ** 12
        m = re.search(r"_s(\d+)x(\d+)_", url)
        if m:
            return int(m.group(1)) * int(m.group(2))
        m = re.search(r"c\d+\.\d+\.\d+\.\d+a.*?_s(\d+)", url)
        if m:
            return int(m.group(1)) ** 2 // 2
        return 0

    def _ig_extract_product_media(self, product_media):
        info = _ig_orig_extract_product_media(self, product_media)
        if info.get("formats"):
            info["is_image"] = False
            return info

        candidates = [
            c
            for c in ((product_media or {}).get("image_versions2") or {}).get("candidates") or []
            if c.get("url")
        ]
        if not candidates:
            return info

        url = max(candidates, key=lambda c: _ig_candidate_score(c.get("url"))).get("url")
        ext = "png" if ".png" in url else ("webp" if ".webp" in url else "jpg")

        info["is_image"] = True
        info["ext"] = ext
        # "_extract_product" labels everything "Video by <user>";
        # rename it when the entry is actually a photo.
        old_title = info.get("title") or ""
        if old_title.startswith("Video by "):
            info["title"] = "Photo by " + old_title[len("Video by "):]
        elif not old_title:
            username = ((product_media or {}).get("user") or {}).get("username")
            info["title"] = f"Photo by {username}" if username else "Photo"
        info["formats"] = [
            {
                "format_id": "photo",
                "format_note": "InstagramPhoto",
                "url": url,
                "ext": ext,
                "width": product_media.get("original_width"),
                "height": product_media.get("original_height"),
                "filesize": None,
                "protocol": "https",
                "http_headers": {"Referer": "https://www.instagram.com/"},
                "_in_manifest": False,
            }
        ]
        return info

    InstagramIE._extract_product_media = _ig_extract_product_media
    IG_PATCH_OK = True
except Exception as _ig_err:  # pragma: no cover
    print(f"WARNING: Instagram photo patch not applied: {_ig_err}")


# ============================================================
#  ENVIRONMENT & CONFIGURATION
# ============================================================

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN is not set in .env file!")
    sys.exit(1)

PROXY_HTTP: str = os.getenv("PROXY_HTTP", "")
PROXY_HTTPS: str = os.getenv("PROXY_HTTPS", "")

PROXY_ENABLED = bool(PROXY_HTTP or PROXY_HTTPS)
_proxy_needs_fallback = False

logger = None  # initialized in the LOGGING section

if PROXY_ENABLED:
    proxy_dict: Dict[str, str] = {}
    if PROXY_HTTP:
        proxy_dict["http"] = PROXY_HTTP
    if PROXY_HTTPS:
        proxy_dict["https"] = PROXY_HTTPS

    try:
        import socket as _proxy_socket
        from urllib.parse import urlparse as _proxy_urlparse

        _test_url = PROXY_HTTPS or PROXY_HTTP
        _parsed = _proxy_urlparse(_test_url)
        _host = _parsed.hostname or "127.0.0.1"
        _port = _parsed.port or 1080
        _sock = _proxy_socket.socket(_proxy_socket.AF_INET, _proxy_socket.SOCK_STREAM)
        _sock.settimeout(3)
        _result = _sock.connect_ex((_host, _port))
        _sock.close()

        if _result == 0:
            apihelper.proxy = proxy_dict
            print(f"Proxy connected: {_host}:{_port}")
        else:
            print(f"WARNING: Proxy {_host}:{_port} is not reachable.")
            print("         Bot will run WITHOUT proxy (direct connection).")
            PROXY_ENABLED = False
            _proxy_needs_fallback = True
    except Exception as _e:
        print(f"WARNING: Proxy check failed: {_e}")
        print("         Bot will run WITHOUT proxy (direct connection).")
        PROXY_ENABLED = False
        _proxy_needs_fallback = True
else:
    print("No proxy configured. Using direct connection.")

# --- Cookie file for authenticated downloads ---
COOKIE_FILE: str = os.path.expanduser(os.getenv("COOKIE_FILE", "cookies.txt"))


def _join_numbered(prefix: str) -> str:
    """Join PREFIX_1 .. PREFIX_N in numeric order.

    Cloud dashboards cap one variable at 1024 characters, so a full
    cookies.txt has to be split across several variables.
    """
    parts: List[str] = []
    n = 1
    while True:
        chunk = os.getenv(f"{prefix}_{n}", "")
        if not chunk:
            break
        parts.append(chunk.strip())
        n += 1
    return "".join(parts)


def _decode_cookie_blob(blob: str) -> str:
    """Decode a gzip+base64 cookies blob; fall back to plain text."""
    payload = blob.encode("ascii", "ignore")
    try:
        return gzip.decompress(base64.b64decode(payload)).decode("utf-8", "replace")
    except Exception:
        pass
    try:
        return base64.b64decode(payload).decode("utf-8", "replace")
    except Exception:
        return blob


def _fetch_cookies_url(url: str) -> str:
    with urllib.request.urlopen(url, timeout=20) as resp:
        return resp.read().decode("utf-8", "replace")


def _looks_like_cookie_file(text: str) -> bool:
    return "Netscape HTTP Cookie File" in text[:400] or ".youtube.com" in text


def _install_cookies(text: str, source: str) -> bool:
    """Write cookies to a temp file and point COOKIE_FILE at it.

    ``newline=""`` keeps the bytes as they arrived: the default text
    mode would translate every "\n" on Windows and a CRLF file coming
    from a URL would end up as "\r\r\n", which reads back different
    from the original.
    """
    global COOKIE_FILE
    if not text or not _looks_like_cookie_file(text):
        print(f"WARNING: cookies from {source} are not a Netscape cookie file - ignored")
        return False
    try:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, prefix="yt_cookies_",
            encoding="utf-8", newline="",
        )
        with handle:
            handle.write(text)
        COOKIE_FILE = handle.name
        n_rows = sum(1 for line in text.splitlines() if line and not line.startswith("#"))
        print(f"YouTube cookies loaded from {source} -> {COOKIE_FILE} ({n_rows} rows)")
        return True
    except Exception as exc:
        print(f"WARNING: failed to write cookie file from {source}: {exc}")
        return False


def _load_cloud_cookies() -> None:
    """Resolve YouTube cookies from whichever source the platform allows.

    Checked in order, first usable one wins:
      1. YOUTUBE_COOKIES_URL     - a URL serving the cookies.txt file
      2. YOUTUBE_COOKIES_B64_1..N (or YOUTUBE_COOKIES_B64) - gzip+base64
      3. YOUTUBE_COOKIES         - raw text (Railway / local .env)
    """
    url = os.getenv("YOUTUBE_COOKIES_URL", "").strip()
    if url:
        try:
            if _install_cookies(_fetch_cookies_url(url), "YOUTUBE_COOKIES_URL"):
                return
        except Exception as exc:
            print(f"WARNING: could not fetch YOUTUBE_COOKIES_URL: {exc}")

    blob = _join_numbered("YOUTUBE_COOKIES_B64") or os.getenv("YOUTUBE_COOKIES_B64", "").strip()
    if blob:
        if _install_cookies(_decode_cookie_blob(blob), "YOUTUBE_COOKIES_B64_*"):
            return

    raw = os.getenv("YOUTUBE_COOKIES", "")
    if raw:
        _install_cookies(raw, "YOUTUBE_COOKIES")


_load_cloud_cookies()

# --- PO Token server URL (bgutil HTTP server) ---
PO_TOKEN_SERVER_URL: str = os.getenv("PO_TOKEN_SERVER_URL", "")

# ============================================================
#  PO TOKEN PROVIDER - HEALTH CHECK
#  The bgutil provider has two flavours:
#    * HTTP server  (PO_TOKEN_SERVER_URL)  - used on Railway
#    * local script (~/bgutil-ytdlp-pot-provider) - used on a PC
#  If either one is broken, yt-dlp aborts the WHOLE download
#  instead of continuing without a token.  We therefore check
#  the provider once and switch it off cleanly when unusable:
#  pointing the script provider at a file that does not exist
#  makes its is_available() return False without spawning a
#  runtime that can hang.
# ============================================================

# Pointing the script provider at a path that cannot exist disables it.
_PO_SCRIPT_OFF: str = "/__bgutil_disabled/generate_once.ts"


def _bgutil_version() -> str:
    try:
        from importlib import metadata as _md
        return _md.version("bgutil-ytdlp-pot-provider")
    except Exception:
        try:
            from yt_dlp_plugins.extractor.getpot_bgutil import __version__
            return str(__version__)
        except Exception:
            return "unknown"


BGUTIL_VERSION: str = _bgutil_version()
_po_status: Dict[str, str] = {"mode": "", "detail": "not checked yet"}


def _check_http_po_server() -> Tuple[bool, str]:
    if not PO_TOKEN_SERVER_URL:
        return False, "PO_TOKEN_SERVER_URL is not set"
    ping = PO_TOKEN_SERVER_URL.rstrip("/") + "/ping"
    try:
        with urllib.request.urlopen(ping, timeout=6) as resp:
            payload = resp.read().decode("utf-8", "replace") or "{}"
        data = json.loads(payload)
    except Exception as exc:
        return False, f"cannot reach {ping}: {exc}"
    version = str(data.get("version") or "")
    if not version:
        return False, "server did not report a version"
    if BGUTIL_VERSION == "unknown":
        # The provider is reachable; a silent "off" here just means
        # YouTube fails later with a confusing bot-check error.
        return True, (
            f"HTTP server {version} (plugin version unknown - "
            f"bgutil-ytdlp-pot-provider not importable)"
        )
    if version.split(".", 1)[0] != BGUTIL_VERSION.split(".", 1)[0]:
        return False, (
            f"version mismatch - plugin {BGUTIL_VERSION} vs server {version}. "
            f"Rebuild with matching versions."
        )
    return True, f"HTTP server {version} (plugin {BGUTIL_VERSION})"


def _check_script_po() -> Tuple[bool, str]:
    home = os.path.expanduser("~/bgutil-ytdlp-pot-provider/server")
    script = None
    for rel in (os.path.join("src", "generate_once.ts"), os.path.join("build", "generate_once.js")):
        candidate = os.path.join(home, rel)
        if os.path.isfile(candidate):
            script = candidate
            break
    if not script:
        return False, f"no script at {home}"

    runtime = shutil.which("deno") or shutil.which("node")
    if not runtime:
        return False, "no deno/node runtime on PATH"
    cmd = [runtime]
    if os.path.basename(runtime).lower().startswith("deno"):
        cmd += ["run", "--allow-env", "--allow-net", "--allow-read",
                "--allow-write", "--allow-ffi"]
    cmd += [script, "--version"]
    try:
        proc = subprocess.run(
            cmd, timeout=10, capture_output=True, text=True,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return False, "script did not answer within 10s"
    except Exception as exc:
        return False, f"script failed to start: {exc}"
    if proc.returncode:
        tail = (proc.stderr or proc.stdout or "").strip()[-200:]
        return False, f"script exit {proc.returncode}: {tail}"
    return True, f"local script ({os.path.basename(script)})"


def check_po_provider(force: bool = False) -> str:
    """Pick the first usable PO token provider: HTTP server, then local script.

    Trying only one and giving up is what silently turns YouTube off, so
    every rejection is recorded and the next candidate still gets a turn.
    """
    if _po_status["mode"] and not force:
        return _po_status["mode"]

    rejected: List[str] = []
    mode, detail, ok = "off", "no PO token provider available", False

    if PO_TOKEN_SERVER_URL:
        http_ok, http_detail = _check_http_po_server()
        if http_ok:
            mode, detail, ok = "http", http_detail, True
        else:
            rejected.append(f"http ({PO_TOKEN_SERVER_URL}): {http_detail}")

    if not ok:
        script_ok, script_detail = _check_script_po()
        if script_ok:
            mode, detail, ok = "script", script_detail, True
        else:
            rejected.append(f"script: {script_detail}")

    if not ok:
        detail = "; ".join(rejected) or "no PO token provider available"

    _po_status["mode"] = mode
    _po_status["detail"] = detail
    level = logging.INFO if ok else logging.WARNING
    logger.log(level, f"PO token provider = {mode}: {detail}")
    return mode


def _apply_po_args(extractor_args: Dict[str, Any]) -> None:
    """Fill extractor_args for the PO provider currently in use.

    Every value MUST be a list. ``InfoExtractor._configuration_arg``
    evaluates ``[x.lower() for x in val]``, so a plain string is iterated
    character by character and ``base_url`` collapses to ``"h"``. The
    plugin then cannot even reach ``/ping`` (``Unsupported url scheme:
    ""``), reports the server unavailable, and NO PO token is requested -
    which is exactly the "confirm you're not a bot" wall.
    """
    mode = check_po_provider()
    if mode == "http":
        extractor_args["youtubepot-bgutilhttp"] = {"base_url": [PO_TOKEN_SERVER_URL]}
        extractor_args["youtubepot-bgutilscript"] = {"script_path": [_PO_SCRIPT_OFF]}
    elif mode == "script":
        extractor_args["youtubepot-bgutilscript"] = {"server_home": [
            os.path.expanduser("~/bgutil-ytdlp-pot-provider/server")]}
    else:
        # provider off - never let it spawn a runtime
        extractor_args["youtubepot-bgutilscript"] = {"script_path": [_PO_SCRIPT_OFF]}

# --- Limits ---
MAX_FILE_SIZE_MB: int = int(os.getenv("MAX_FILE_SIZE_MB", "50"))
MAX_FILE_SIZE_BYTES: int = MAX_FILE_SIZE_MB * 1024 * 1024
RATE_LIMIT_WINDOW: int = int(os.getenv("RATE_LIMIT_WINDOW", "30"))
MAX_REQUESTS_PER_WINDOW: int = int(os.getenv("MAX_REQUESTS_PER_WINDOW", "5"))
MAX_PHOTOS_PER_POST: int = int(os.getenv("MAX_PHOTOS_PER_POST", "30"))
UPLOAD_TIMEOUT: int = int(os.getenv("UPLOAD_TIMEOUT", "600"))

# --- Paths ---
DOWNLOAD_DIR: str = os.path.expanduser(os.getenv("DOWNLOAD_DIR", "~/downloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ============================================================
#  LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("MediaDownloaderBot")
logging.getLogger("yt_dlp").setLevel(logging.WARNING)

# ============================================================
#  FFMPEG CHECK
# ============================================================

FFMPEG_AVAILABLE: bool = shutil.which("ffmpeg") is not None
if not FFMPEG_AVAILABLE:
    logger.warning(
        "ffmpeg NOT found! Audio extraction & format merging will fail. "
        "Install: sudo apt install ffmpeg (Linux) or brew install ffmpeg (macOS)"
    )
else:
    logger.info(f"ffmpeg found at: {shutil.which('ffmpeg')}")

logger.info(f"Instagram photo patch: {'APPLIED' if IG_PATCH_OK else 'FAILED'}")

# ============================================================
#  BOT INITIALIZATION
# ============================================================

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

try:
    bot.remove_webhook()
except Exception:
    pass

# ============================================================
#  DATA STRUCTURES (thread-safe)
# ============================================================

user_states: Dict[int, Dict[str, Any]] = {}
_states_lock = threading.Lock()

rate_limit_map: Dict[int, list] = {}
_rate_lock = threading.Lock()

# ============================================================
#  STATE / RATE LIMIT HELPERS
# ============================================================


def cleanup_stale_states(max_age_seconds: int = 1800) -> None:
    now = datetime.now()
    with _states_lock:
        stale = [
            cid
            for cid, s in user_states.items()
            if (now - s.get("timestamp", now)).total_seconds() > max_age_seconds
        ]
        for cid in stale:
            del user_states[cid]
    if stale:
        logger.info(f"Cleaned up {len(stale)} stale user states")


def cleanup_rate_map(max_age_seconds: int = 3600) -> None:
    """Drop idle users from the rate-limit map so it cannot grow forever."""
    now = datetime.now()
    with _rate_lock:
        idle = [
            cid
            for cid, stamps in rate_limit_map.items()
            if not stamps or (now - max(stamps)).total_seconds() > max_age_seconds
        ]
        for cid in idle:
            del rate_limit_map[cid]


def check_rate_limit(chat_id: int) -> bool:
    now = datetime.now()
    with _rate_lock:
        if chat_id not in rate_limit_map:
            rate_limit_map[chat_id] = []
        rate_limit_map[chat_id] = [
            t for t in rate_limit_map[chat_id]
            if (now - t).total_seconds() < RATE_LIMIT_WINDOW
        ]
        if len(rate_limit_map[chat_id]) >= MAX_REQUESTS_PER_WINDOW:
            return False
        rate_limit_map[chat_id].append(now)
        return True


# ============================================================
#  URL VALIDATION / EXTRACTION
# ============================================================

SUPPORTED_DOMAINS = re.compile(
    r"https?://("
    r"(www\.)?(youtube\.com|youtu\.be|m\.youtube\.com)"
    r"|(www\.)?(instagram\.com)"
    r"|(www\.)?(tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)"
    r"|(www\.)?(twitter\.com|x\.com)"
    r"|(www\.)?(vimeo\.com)"
    r"|(www\.)?(facebook\.com|fb\.watch)"
    r"|(www\.)?(reddit\.com)"
    r"|(www\.)?(twitch\.tv)"
    r"|(www\.)?(dailymotion\.com)"
    r"|(www\.)?(bilibili\.com)"
    r")",
    re.IGNORECASE,
)

# Finds the first http(s) URL inside an arbitrary message.
# Handles "@url:https://...", backticks, <angle brackets> and trailing punctuation.
URL_FINDER = re.compile(r"https?://[^\s<>\"'`\)\]\}]+", re.IGNORECASE)

_TRAILING_JUNK = ".,;:!?\")]'`}"


def extract_url(text: str) -> str:
    """Pull the first real URL out of a message; return '' when there is none."""
    if not text:
        return ""
    match = URL_FINDER.search(text)
    if not match:
        return ""
    url = match.group(0).rstrip(_TRAILING_JUNK)
    return url


def validate_url(url: str) -> bool:
    if not url:
        return False
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if SUPPORTED_DOMAINS.match(url):
        return True
    return bool(re.match(r"^https?://[^\s/]+\.[^\s/]+", url))


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _is_youtube_url(url: str) -> bool:
    return bool(re.search(r"(youtube\.com|youtu\.be|m\.youtube\.com)", url, re.IGNORECASE))


def _is_instagram_url(url: str) -> bool:
    return bool(re.search(r"(instagram\.com|instagr\.am)", url, re.IGNORECASE))


def esc(text: Any) -> str:
    """Escape text for Telegram HTML parse mode.

    Unescaped '&' or '<' in a video title makes Telegram reject the whole
    message, which looks like a download failure to the user."""
    return html.escape(str(text), quote=False)


# ============================================================
#  YT-DLP CONFIGURATION
# ============================================================

# YouTube player-client tiers, tried in order.
# None means "let yt-dlp choose its own defaults".
#
# With a working PO token the web-family clients are the best;
# without one they trigger the "confirm you're not a bot" wall, so
# we switch the order around (defaults / android first).
TIERS_WITH_PO: List[Optional[List[str]]] = [
    ["mweb", "web"],
    ["tv", "web_safari"],
    ["web_creator", "web"],
    None,
    ["android"],
    ["ios"],
]

TIERS_WITHOUT_PO: List[Optional[List[str]]] = [
    None,
    ["android"],
    ["mweb", "web"],
    ["ios"],
    ["tv", "web_safari"],
]


def client_tiers() -> List[Optional[List[str]]]:
    """Ordered player-client tiers for the current PO provider state."""
    return TIERS_WITH_PO if check_po_provider() in ("http", "script") else TIERS_WITHOUT_PO


def tier_label(clients: Optional[List[str]]) -> str:
    return "yt-dlp defaults" if clients is None else "+".join(clients)

_USER_AGENTS = {
    "mweb": (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Mobile Safari/537.36"
    ),
    "web_creator": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "web_safari": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.4 Safari/605.1.15"
    ),
    "tv": "Mozilla/5.0 (QtEmbedded; U; Linux) AppleWebKit/537.36",
    "web": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    ),
    "android": (
        "com.google.android.youtube/19.09.37 (Linux; U; Android 14; en_US; "
        "Pixel 8 Pro; Build/UP1A.231105.001) gzip"
    ),
    "ios": "com.google.ios.youtube/19.09.3 (iPhone14,3; U; CPU iOS 17_4 like Mac OS X; en_US)",
}

def build_ydl_opts(
    media_type: str,
    quality: str,
    clients: Optional[List[str]] = None,
    output_dir: str = DOWNLOAD_DIR,
) -> Dict[str, Any]:
    """Build yt-dlp options.

    ``media_type``: video | audio | image
    ``clients``   : YouTube player-client list, or None for yt-dlp defaults.
    """
    ua = _USER_AGENTS.get((clients or ["web"])[0], _USER_AGENTS["web"])

    opts: Dict[str, Any] = {
        "outtmpl": os.path.join(output_dir, "%(title).100s_%(id)s.%(ext)s"),
        # image mode must keep every carousel item
        "noplaylist": media_type != "image",
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        # ignoreerrors=True swallows the real failure and returns None,
        # which the old code then reported as "yt-dlp returned no info".
        "ignoreerrors": False,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_args": {},
        "user_agent": ua,
        "http_headers": {
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    }

    # --- Proxy for yt-dlp (NOT for the PO token server) ---
    yt_proxy = os.getenv("YTDLP_PROXY", "")
    if yt_proxy:
        opts["proxy"] = yt_proxy

    if clients is not None:
        opts["extractor_args"]["youtube"] = {"player_client": clients}

    # --- PO Token provider (must never wipe the args set above) ---
    _apply_po_args(opts["extractor_args"])

    # --- Cookies ---
    if os.path.exists(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE

    # --- Media type specific ---
    if media_type == "audio":
        opts["format"] = "bestaudio/best"
        if FFMPEG_AVAILABLE:
            opts["postprocessors"] = [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": quality,
                }
            ]
        else:
            opts["format"] = "bestaudio[ext=m4a]/bestaudio"
    elif media_type == "image":
        # Instagram photo formats are a single JPEG; keep it simple.
        opts["format"] = "best"
    else:
        # Video: honour the requested height, then degrade gracefully.
        try:
            height = max(144, min(int(quality), 4320))
        except (TypeError, ValueError):
            height = 1080
        opts["format"] = (
            f"bv*[height<={height}]+ba/b[height<={height}]/"
            f"bv*+ba/b/b[height<={height}]/best"
        )
        opts["merge_output_format"] = "mp4"

    opts["progress_hooks"] = []
    return opts


# ============================================================
#  PROGRESS HANDLING
# ============================================================

def make_progress_hook(chat_id: int, status_message_id: int, index: int = 1, total: int = 1):
    last_update_time = [0.0]

    def progress_hook(d: dict) -> None:
        now = time.time()
        if now - last_update_time[0] < 2.0:
            return
        last_update_time[0] = now

        status = d.get("status", "")
        if status == "downloading":
            total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            eta = d.get("eta") or 0

            if total_bytes > 0:
                pct = min(100, max(0, int(downloaded / total_bytes * 100)))
                bar = _make_bar(pct)
                size_mb = total_bytes / (1024 * 1024)
                speed_str = f"{speed / 1024 / 1024:.1f} MB/s" if speed else "calculating..."
                eta_str = str(timedelta(seconds=int(eta))) if eta else "..."
                item = f"File {index} of {total}\n" if total > 1 else ""
                text = (
                    f"<b>Downloading...</b>\n\n"
                    f"{item}{bar} <b>{pct}%</b>\n"
                    f"Size: <code>{size_mb:.1f} MB</code>\n"
                    f"Speed: <code>{esc(speed_str)}</code>\n"
                    f"ETA: <code>{eta_str}</code>"
                )
                _safe_edit(chat_id, status_message_id, text)

        elif status == "finished":
            item = f"File {index} of {total}\n" if total > 1 else ""
            _safe_edit(
                chat_id, status_message_id,
                f"<b>Processing...</b>\n{item}Merging formats &amp; converting...",
            )

    return progress_hook


def _make_bar(percent: int, length: int = 14) -> str:
    filled = int(length * percent / 100)
    empty = length - filled
    return "\u2595" + "\u25B0" * filled + "\u25B1" * empty + "\u258f"


def _safe_edit(chat_id: int, message_id: int, text: str) -> None:
    try:
        bot.edit_message_text(
            text, chat_id=chat_id, message_id=message_id, parse_mode="HTML",
        )
    except Exception:
        pass


def _safe_send(chat_id: int, text: str) -> None:
    """Send a plain HTML message; never raise."""
    try:
        bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception:
        try:
            bot.send_message(chat_id, re.sub(r"<[^>]+>", "", text))
        except Exception:
            pass


# ============================================================
#  URL PROBE (what does this post actually contain?)
# ============================================================

def _probe_opts() -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": False,
        "ignoreerrors": False,
        "skip_download": True,
        "socket_timeout": 30,
        "retries": 2,
        "extractor_args": {},
        "http_headers": {
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.instagram.com/",
        },
    }
    _apply_po_args(opts["extractor_args"])
    if os.path.exists(COOKIE_FILE):
        opts["cookiefile"] = COOKIE_FILE
    return opts


def probe_url(url: str) -> Dict[str, Any]:
    """Return {'ok', 'error', 'images', 'videos', 'total', 'is_image_post'}.

    Used to show the user only the buttons that make sense for the link."""
    result = {"ok": True, "error": "", "images": 0, "videos": 0, "total": 1, "is_image_post": False}
    try:
        with yt_dlp.YoutubeDL(_probe_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        result["ok"] = False
        result["error"] = str(e)[:400]
        return result

    if info is None:
        result["ok"] = False
        result["error"] = "The extractor returned no data."
        return result

    entries = info.get("entries")
    if not entries:
        entries = [info]
    entries = [e for e in entries if e]

    result["total"] = len(entries)
    for entry in entries:
        if entry.get("is_image"):
            result["images"] += 1
        else:
            result["videos"] += 1
    result["is_image_post"] = result["images"] > 0 and result["videos"] == 0
    return result


# ============================================================
#  FILE SENDING
# ============================================================

def _size_guard(chat_id: int, file_path: str) -> bool:
    file_size = os.path.getsize(file_path)
    if file_size <= MAX_FILE_SIZE_BYTES:
        return True
    size_mb = file_size / (1024 * 1024)
    _safe_send(
        chat_id,
        f"<b>File too large!</b>\n\n"
        f"Size: <code>{size_mb:.1f} MB</code>\n"
        f"Telegram limit: <code>{MAX_FILE_SIZE_MB} MB</code>\n\n"
        f"<i>Try a lower quality or the audio-only format.</i>",
    )
    return False


def send_file_safely(
    chat_id: int, file_path: str, media_type: str, title: str = "Unknown",
) -> bool:
    if not os.path.exists(file_path):
        _safe_send(chat_id, "<b>File error:</b> the downloaded file is missing.")
        return False
    if not _size_guard(chat_id, file_path):
        return False

    caption = esc(title)[:1000]
    try:
        with open(file_path, "rb") as f:
            if media_type == "audio":
                bot.send_audio(chat_id, f, title=str(title)[:64], timeout=UPLOAD_TIMEOUT)
            elif media_type == "image":
                bot.send_photo(chat_id, f, caption=caption, timeout=UPLOAD_TIMEOUT)
            else:
                bot.send_video(
                    chat_id, f,
                    caption=caption,
                    timeout=UPLOAD_TIMEOUT,
                    supports_streaming=True,
                )
        return True
    except Exception as e:
        logger.error(f"Failed to send file to {chat_id}: {e}")
        # Retry once as a plain document - survives Telegram entity errors.
        try:
            with open(file_path, "rb") as f:
                bot.send_document(chat_id, f, caption=caption, timeout=UPLOAD_TIMEOUT)
            return True
        except Exception as e2:
            logger.error(f"Document fallback failed for {chat_id}: {e2}")
            _safe_send(
                chat_id,
                f"<b>Failed to send the file.</b>\n<code>{esc(str(e2))[:400]}</code>",
            )
            return False


PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v", ".mkv", ".avi"}


def _media_kind(file_path: str) -> str:
    """photo | video | document - decides how Telegram should receive it."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext in PHOTO_EXTS:
        return "photo"
    if ext in VIDEO_EXTS:
        return "video"
    return "document"


def _send_single(chat_id: int, file_path: str, title: str) -> bool:
    """Send one downloaded item, choosing the right Telegram method."""
    if not os.path.exists(file_path) or not _size_guard(chat_id, file_path):
        return False
    caption = esc(title)[:1000]
    kind = _media_kind(file_path)
    try:
        with open(file_path, "rb") as fh:
            if kind == "video":
                bot.send_video(chat_id, fh, caption=caption, timeout=UPLOAD_TIMEOUT,
                               supports_streaming=True)
            elif kind == "document":
                bot.send_document(chat_id, fh, caption=caption, timeout=UPLOAD_TIMEOUT)
            else:
                bot.send_photo(chat_id, fh, caption=caption, timeout=UPLOAD_TIMEOUT)
        return True
    except Exception as e:
        logger.warning(f"_send_single({kind}) failed for {file_path}: {e}")
        try:
            with open(file_path, "rb") as fh:
                bot.send_document(chat_id, fh, caption=caption, timeout=UPLOAD_TIMEOUT)
            return True
        except Exception as e2:
            logger.error(f"document fallback failed for {file_path}: {e2}")
            return False


def send_photos(chat_id: int, paths: List[str], title: str = "") -> bool:
    """Send downloaded items as Telegram media groups (max 10 per group).

    A carousel can mix photos and videos.  Telegram media groups accept
    photo+video together but never documents, so documents are flushed on
    their own and the rest go out as albums."""
    if not paths:
        return False

    ok = True
    caption = esc(title)[:1000]
    pending: List[str] = []

    def flush(items: List[str]) -> None:
        nonlocal ok
        if not items:
            return
        handles = []
        try:
            media = []
            for index, path in enumerate(items):
                fh = open(path, "rb")
                handles.append(fh)
                kwargs = {"caption": caption} if (index == 0 and caption) else {}
                if _media_kind(path) == "video":
                    media.append(InputMediaVideo(fh, **kwargs))
                else:
                    media.append(InputMediaPhoto(fh, **kwargs))
            bot.send_media_group(chat_id, media)
        except Exception as e:
            # A group failure is not itself a lost item: retry each file alone
            # and only report failure if a single send fails too.
            logger.warning(f"send_media_group failed ({e}); sending items one by one")
            for path in items:
                if not _send_single(chat_id, path, title or "Instagram post"):
                    ok = False
        finally:
            for fh in handles:
                try:
                    fh.close()
                except Exception:
                    pass

    for path in paths:
        if not os.path.exists(path):
            ok = False
            continue
        if _media_kind(path) == "document":
            if pending:
                flush(pending)
                pending = []
            if not _send_single(chat_id, path, title or "Instagram post"):
                ok = False
            continue
        if not _size_guard(chat_id, path):
            ok = False
            continue
        pending.append(path)
        if len(pending) >= 10:
            flush(pending)
            pending = []

    flush(pending)
    return ok


# ============================================================
#  YT-DLP RUN HELPERS
# ============================================================

def _determine_file_path(ydl: yt_dlp.YoutubeDL, info: dict, media_type: str) -> Optional[str]:
    file_path = None
    if info.get("requested_downloads"):
        file_path = info["requested_downloads"][0].get("filepath") or None
    if not file_path:
        file_path = ydl.prepare_filename(info)

    if media_type == "audio" and file_path:
        base = os.path.splitext(file_path)[0]
        for ext in (".mp3", ".m4a", ".opus", ".aac", ".webm"):
            candidate = base + ext
            if os.path.exists(candidate):
                return candidate
    return file_path


def _collect_paths(ydl: yt_dlp.YoutubeDL, info: dict, media_type: str) -> List[str]:
    """Gather every downloaded file (single post or carousel/playlist)."""
    entries = info.get("entries")
    if not entries:
        entries = [info]

    paths: List[str] = []
    for entry in entries:
        if not entry:
            continue
        path = None
        if entry.get("requested_downloads"):
            path = entry["requested_downloads"][0].get("filepath") or None
        if not path:
            try:
                path = ydl.prepare_filename(entry)
            except Exception:
                path = None
        if media_type == "audio" and path:
            base = os.path.splitext(path)[0]
            for ext in (".mp3", ".m4a", ".opus", ".aac", ".webm"):
                candidate = base + ext
                if os.path.exists(candidate):
                    path = candidate
                    break
        if path and os.path.exists(path) and path not in paths:
            paths.append(path)
    return paths


def _run_ytdlp(
    url: str,
    media_type: str,
    quality: str,
    clients: Optional[List[str]],
    progress_hook=None,
):
    """One yt-dlp attempt. Returns (paths, title, ydl_info)."""
    opts = build_ydl_opts(media_type, quality, clients=clients)
    if progress_hook is not None:
        opts["progress_hooks"] = [progress_hook]

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            raise ValueError("yt-dlp returned no info")

        paths = _collect_paths(ydl, info, media_type)
        title = (
            info.get("title")
            or info.get("fulltitle")
            or info.get("alt_title")
            or (info.get("entries") or [{}])[0].get("title")
            or "Unknown"
        )

        if not paths:
            # last-chance single-file resolution
            single = _determine_file_path(ydl, info, media_type)
            if single and os.path.exists(single):
                paths = [single]
        if not paths:
            raise FileNotFoundError("Download finished but no file was produced")

    return paths, str(title), info


def run_download(
    url: str,
    media_type: str,
    quality: str,
    progress_hook=None,
):
    """Download with retry/fallback strategy.

    YouTube: walk the player-client tiers (PO-aware).  Any failure -
    including a crash inside the PO token provider - moves on to the
    next tier instead of killing the download.  Everything else runs
    a single attempt, since player_client only affects YouTube."""
    if _is_youtube_url(url):
        tiers: List[Optional[List[str]]] = list(client_tiers())
    else:
        tiers = [None]

    last_error: Optional[Exception] = None
    tried: List[Optional[List[str]]] = []
    while tiers:
        clients = tiers.pop(0)
        tried.append(clients)
        label = tier_label(clients)
        logger.info(f"yt-dlp attempt {len(tried)} [{label}]")
        try:
            return _run_ytdlp(url, media_type, quality, clients, progress_hook)
        except Exception as e:
            last_error = e
            logger.warning(
                f"attempt {len(tried)} [{label}] {type(e).__name__}: {str(e)[:250]}"
            )
            if _is_po_crash(str(e)) and check_po_provider() in ("http", "script"):
                # The provider itself blew up. Turn it off for good and
                # walk the PO-free tiers instead of dying with it.
                logger.warning("PO token provider crashed - switching it off")
                _po_status["mode"] = "off"
                _po_status["detail"] = "disabled after a provider crash"
                tiers = [t for t in TIERS_WITHOUT_PO if t not in tried]
                continue
            if _is_impossible_error(str(e)):
                break

    if last_error is not None:
        raise last_error
    raise RuntimeError("Download failed for an unknown reason")


def _is_impossible_error(error_text: str) -> bool:
    """Errors where every other player-client will fail the same way."""
    low = (error_text or "").lower()
    return any(marker in low for marker in (
        "private video",
        "video unavailable",
        "account associated with this video has been terminated",
        "has been removed",
        "not available in your country",
    ))


def _is_po_crash(error_text: str) -> bool:
    low = (error_text or "").lower()
    return any(marker in low for marker in (
        "bgutil", "generate_once", "potoken", "po token", "pot provider",
        "deno", "timeoutexpired",
    ))


# ============================================================
#  MAIN DOWNLOAD LOGIC
# ============================================================

def download_and_send(chat_id: int, status_msg_id: int) -> None:
    with _states_lock:
        state = user_states.get(chat_id, {}).copy()

    url = state.get("url", "")
    media_type = state.get("media_type", "video")
    quality = state.get("quality", "720")

    if not url:
        _safe_edit(chat_id, status_msg_id, "Session expired. Send a new link.")
        return

    paths: List[str] = []
    title = "Unknown"

    try:
        progress_hook = make_progress_hook(chat_id, status_msg_id)
        paths, title, _info = run_download(url, media_type, quality, progress_hook)

        total_mb = sum(os.path.getsize(p) for p in paths) / (1024 * 1024)
        quality_label = (
            f"{quality} kbps" if media_type == "audio" else (f"{quality}p" if media_type == "video" else "original")
        )

        if media_type == "image":
            _safe_edit(
                chat_id, status_msg_id,
                f"<b>Download complete!</b>\n"
                f"Photos: <code>{len(paths)}</code>\n"
                f"Size: <code>{total_mb:.1f} MB</code>\n"
                f"<b>Sending to Telegram...</b>",
            )
            ok = send_photos(chat_id, paths, title)
        else:
            _safe_edit(
                chat_id, status_msg_id,
                f"<b>Download complete!</b>\n"
                f"Size: <code>{total_mb:.1f} MB</code>\n"
                f"<b>Sending to Telegram...</b>",
            )
            ok = send_file_safely(chat_id, paths[0], media_type, title)

        if ok:
            noun = f"{len(paths)} photo(s)" if media_type == "image" else f"{total_mb:.1f} MB"
            _safe_send(
                chat_id,
                f"<b>Done!</b>\n\n"
                f"Title: <b>{esc(title)[:200]}</b>\n"
                f"Sent: <code>{esc(noun)}</code>\n"
                f"Quality: <code>{esc(quality_label)}</code>\n\n"
                f"Send another link for a new download.",
            )

    except yt_dlp.utils.DownloadError as e:
        error_msg = str(e)[:800]
        logger.error(f"yt-dlp error for {chat_id}: {error_msg}")
        _safe_edit(chat_id, status_msg_id, _friendly_error(error_msg))
    except (FileNotFoundError, ValueError) as e:
        logger.error(f"File error for {chat_id}: {e}")
        _safe_edit(chat_id, status_msg_id, f"<b>File error:</b>\n<code>{esc(str(e))[:500]}</code>")
    except Exception as e:
        logger.error(f"Unexpected error for {chat_id}: {e}", exc_info=True)
        _safe_edit(
            chat_id, status_msg_id,
            f"<b>Unexpected error:</b>\n<code>{esc(str(e))[:500]}</code>",
        )
    finally:
        for path in paths:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError as e:
                    logger.warning(f"Could not delete {path}: {e}")
        _cleanup_download_dir()


def _friendly_error(message: str) -> str:
    """Turn a raw yt-dlp error into a short, actionable HTML message."""
    low = message.lower()
    hint = ""
    if "sign in" in low or "not a bot" in low or "login_required" in low:
        hint = (
            "YouTube is asking for a login from this server.\n"
            "Fix: refresh YOUTUBE_COOKIES in the Railway environment variables."
        )
    elif "requested format is not available" in low:
        hint = "That quality does not exist for this video. Pick a lower quality."
    elif "private video" in low or "members-only" in low:
        hint = "The content is private or members-only. Cookies for that account are required."
    elif "no video formats found" in low or "no video in this post" in low:
        hint = "This post has no video. Use the Photos button instead."
    elif "confirm" in low and "bot" in low:
        hint = "Bot check. A PO token server plus fresh cookies is required on Railway."
    elif "unable to download webpage" in low or "timed out" in low:
        hint = "Network problem. Check YTDLP_PROXY or try again."

    body = (
        f"<b>Download failed.</b>\n\n"
        f"<code>{esc(message[:400])}</code>"
    )
    if hint:
        # hint is a static string defined above, safe as HTML
        body += f"\n\n<i>{hint}</i>"
    return body


def _cleanup_download_dir() -> None:
    try:
        now = time.time()
        for fname in os.listdir(DOWNLOAD_DIR):
            fpath = os.path.join(DOWNLOAD_DIR, fname)
            if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > 3600:
                try:
                    os.remove(fpath)
                except OSError:
                    pass
    except Exception:
        pass


# ============================================================
#  BOT HANDLERS
# ============================================================

def _main_menu_markup() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("Supported Sites", callback_data="info_sites"),
        InlineKeyboardButton("How to Use", callback_data="info_usage"),
        InlineKeyboardButton("Cookie Setup", callback_data="info_cookies"),
    )
    return markup


def _format_markup(has_images: bool, image_count: int, has_video: bool) -> InlineKeyboardMarkup:
    """Only offer the options that exist for this link."""
    markup = InlineKeyboardMarkup(row_width=2)
    if has_video:
        markup.add(
            InlineKeyboardButton("Video", callback_data="type_video"),
            InlineKeyboardButton("Audio (MP3)", callback_data="type_audio"),
        )
    if has_images:
        label = "Photos" if image_count <= 1 else f"Photos ({image_count})"
        markup.add(InlineKeyboardButton(f"\U0001F4F7 {label}", callback_data="type_image"))
    markup.add(InlineKeyboardButton("Back", callback_data="type_back"))
    return markup


def _quality_markup(media_type: str) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    if media_type == "audio":
        markup.add(
            InlineKeyboardButton("MP3 128 kbps", callback_data="q_128"),
            InlineKeyboardButton("MP3 192 kbps", callback_data="q_192"),
            InlineKeyboardButton("MP3 256 kbps", callback_data="q_256"),
            InlineKeyboardButton("MP3 320 kbps", callback_data="q_320"),
        )
    else:
        markup.add(
            InlineKeyboardButton("480p", callback_data="q_480"),
            InlineKeyboardButton("720p", callback_data="q_720"),
            InlineKeyboardButton("1080p", callback_data="q_1080"),
            InlineKeyboardButton("1440p (2K)", callback_data="q_1440"),
            InlineKeyboardButton("2160p (4K)", callback_data="q_2160"),
        )
    markup.add(InlineKeyboardButton("Back", callback_data="type_back"))
    return markup


def _start_download(chat_id: int, message_id: int) -> None:
    thread = threading.Thread(
        target=download_and_send,
        args=(chat_id, message_id),
        daemon=True,
    )
    thread.start()


@bot.message_handler(commands=["start", "help"])
def handle_start(message: telebot.types.Message) -> None:
    chat_id = message.chat.id
    cookies_status = "Enabled" if os.path.exists(COOKIE_FILE) else "Not configured"
    ffmpeg_status = "Ready" if FFMPEG_AVAILABLE else "MISSING"
    po_mode = check_po_provider()
    pot_status = po_mode if po_mode != "off" else "OFF"

    bot.send_message(
        chat_id,
        (
            f"<b>Welcome to Universal Media Downloader!</b>\n\n"
            f"I download from <b>YouTube, Instagram (video + photos + carousels), "
            f"TikTok, Twitter/X</b> and <b>1000+</b> other sites.\n\n"
            f"<b>Just send me a link to get started!</b>\n\n"
            f"Max file size: <code>{MAX_FILE_SIZE_MB} MB</code>\n"
            f"Cookies: <code>{cookies_status}</code>\n"
            f"PO Token: <code>{pot_status}</code>\n"
            f"FFmpeg: <code>{ffmpeg_status}</code>"
        ),
        reply_markup=_main_menu_markup(),
    )


@bot.message_handler(commands=["status"])
def handle_status(message: telebot.types.Message) -> None:
    chat_id = message.chat.id
    with _states_lock:
        active_sessions = len(user_states)

    cookies_status = "Present" if os.path.exists(COOKIE_FILE) else "Not found"
    proxy_status = (
        "Connected" if (PROXY_ENABLED and not _proxy_needs_fallback)
        else "Fallback (direct)" if _proxy_needs_fallback
        else "Not set"
    )

    bot.send_message(
        chat_id,
        (
            f"<b>Bot Status</b>\n\n"
            f"Bot: <b>Online</b>\n"
            f"yt-dlp: <code>{esc(yt_dlp.version.__version__)}</code>\n"
            f"Instagram photos: <code>{'ON' if IG_PATCH_OK else 'OFF'}</code>\n"
            f"Active sessions: <code>{active_sessions}</code>\n"
            f"FFmpeg: <code>{'Present' if FFMPEG_AVAILABLE else 'MISSING'}</code>\n"
            f"Cookies: <code>{esc(cookies_status)}</code>\n"
            f"PO token provider: <code>{esc(check_po_provider())}</code>\n"
            f"PO token detail: <code>{esc(_po_status['detail'])}</code>\n"
            f"bgutil plugin: <code>{esc(BGUTIL_VERSION)}</code>\n"
            f"Proxy: <code>{esc(proxy_status)}</code>\n"
            f"Download dir: <code>{esc(DOWNLOAD_DIR)}</code>\n"
        ),
    )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("info_") and call.data != "info_back"
)
def handle_info_callbacks(call: telebot.types.CallbackQuery) -> None:
    chat_id = call.message.chat.id
    data = call.data

    if data == "info_sites":
        text = (
            "<b>Supported Sites (partial list):</b>\n\n"
            "- YouTube (videos, Shorts, audio)\n"
            "- <b>Instagram</b> (posts, <b>photos</b>, <b>carousels</b>, Reels, Stories*)\n"
            "- TikTok (videos)\n"
            "- Twitter/X (videos + images)\n"
            "- Facebook (videos)\n"
            "- Vimeo, Dailymotion, Twitch\n"
            "- Reddit, Bilibili, and 1000+ more\n\n"
            "<i>* Private/authenticated content needs cookies.</i>"
        )
    elif data == "info_usage":
        text = (
            "<b>How to Use:</b>\n\n"
            "1. Send any media URL (surrounding text is fine)\n"
            "2. Pick Video, Audio or Photos\n"
            "3. Pick quality (photos skip this step)\n"
            "4. Wait for the download\n\n"
            "<b>Commands:</b>\n"
            "/start - welcome message\n"
            "/status - bot health check\n\n"
            f"<b>Limits:</b> {MAX_FILE_SIZE_MB} MB per file, "
            f"{MAX_PHOTOS_PER_POST} photos per post\n"
            f"<b>Rate limit:</b> {MAX_REQUESTS_PER_WINDOW} per {RATE_LIMIT_WINDOW}s"
        )
    elif data == "info_cookies":
        text = (
            "<b>Cookie Setup Guide:</b>\n\n"
            "For private Instagram posts, age-restricted YouTube, and "
            "datacenter IPs (Railway) that trigger bot checks.\n\n"
            "<b>Method 1 - Browser Extension:</b>\n"
            "Install 'Get cookies.txt LOCALLY' (Chrome/Firefox)\n"
            "- Visit the site and log in\n"
            "- Export cookies.txt\n\n"
            "<b>Method 2 - Railway environment variable:</b>\n"
            "Set <code>YOUTUBE_COOKIES</code> to the full cookies.txt content.\n\n"
            "<b>Method 3 - Local file:</b>\n"
            "Place cookies.txt next to bot.py and set <code>COOKIE_FILE</code>.\n\n"
            "<i>Cookies expire - refresh them when downloads start failing.</i>"
        )
    else:
        text = "Unknown info."

    try:
        bot.edit_message_text(text, chat_id, call.message.message_id, parse_mode="HTML")
        back_markup = InlineKeyboardMarkup()
        back_markup.add(InlineKeyboardButton("Back to Menu", callback_data="info_back"))
        bot.edit_message_reply_markup(
            chat_id, call.message.message_id, reply_markup=back_markup
        )
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "info_back")
def handle_back(call: telebot.types.CallbackQuery) -> None:
    chat_id = call.message.chat.id
    try:
        bot.edit_message_text(
            "<b>Universal Media Downloader</b>\n\nSelect a topic to learn more:",
            chat_id, call.message.message_id,
            parse_mode="HTML", reply_markup=_main_menu_markup(),
        )
    except Exception:
        pass
    bot.answer_callback_query(call.id)


# ------------------------------------------------------------
#  LINK RECEIVED
# ------------------------------------------------------------

@bot.message_handler(func=lambda message: True)
def handle_link(message: telebot.types.Message) -> None:
    chat_id = message.chat.id
    raw_text = message.text or message.caption or ""

    if not check_rate_limit(chat_id):
        bot.reply_to(
            message,
            f"<b>Slow down!</b> Max {MAX_REQUESTS_PER_WINDOW} downloads "
            f"per {RATE_LIMIT_WINDOW} seconds.\nPlease wait...",
        )
        return

    cleanup_stale_states()
    cleanup_rate_map()

    url = extract_url(raw_text)
    if not validate_url(url):
        bot.reply_to(
            message,
            (
                "<b>No valid link found.</b>\n\n"
                "Send a direct link to a video, photo or post from:\n"
                "- YouTube, Instagram, TikTok, Twitter/X\n"
                "- Vimeo, Facebook, Dailymotion, etc.\n\n"
                "Example:\n<code>https://www.youtube.com/watch?v=dQw4w9WgXcQ</code>"
            ),
        )
        return

    url = normalize_url(url)
    display_url = esc(url[:120]) + ("..." if len(url) > 120 else "")

    with _states_lock:
        user_states[chat_id] = {
            "url": url,
            "timestamp": datetime.now(),
            "media_type": None,
            "quality": None,
            "images": 0,
            "videos": 1,
        }

    # Instagram: inspect the post so we only offer buttons that exist.
    if _is_instagram_url(url):
        status_msg = bot.reply_to(
            message,
            f"<b>Link received!</b>\n<code>{display_url}</code>\n\n"
            f"<i>Checking the post...</i>",
        )
        probe = probe_url(url)
        with _states_lock:
            if chat_id in user_states:
                user_states[chat_id]["images"] = probe.get("images", 0)
                user_states[chat_id]["videos"] = probe.get("videos", 0)

        if not probe["ok"]:
            _safe_edit(
                chat_id, status_msg.message_id,
                f"<b>Link received!</b>\n<code>{display_url}</code>\n\n"
                f"<b>Could not read this post:</b>\n"
                f"<code>{esc(probe['error'][:300])}</code>\n\n"
                f"<i>It may be private, deleted, or need fresh cookies.</i>",
            )
            return

        summary = []
        if probe["images"]:
            summary.append(f"Photos: <b>{probe['images']}</b>")
        if probe["videos"]:
            summary.append(f"Videos: <b>{probe['videos']}</b>")

        summary_block = "\n".join(summary)
        _safe_edit(
            chat_id, status_msg.message_id,
            f"<b>Link received!</b>\n<code>{display_url}</code>\n\n"
            f"{summary_block}\n\n"
            f"<b>Choose what to download:</b>",
        )
        try:
            bot.edit_message_reply_markup(
                chat_id, status_msg.message_id,
                reply_markup=_format_markup(
                    probe["images"] > 0, probe["images"], probe["videos"] > 0
                ),
            )
        except Exception:
            pass
        return

    bot.reply_to(
        message,
        f"<b>Link received!</b>\n<code>{display_url}</code>\n\n"
        f"<b>Choose output format:</b>",
        reply_markup=_format_markup(False, 0, True),
    )


# ------------------------------------------------------------
#  RECOVERY HELPERS
#  An inline button can outlive the in-memory session: the process
#  restarted, the session timed out, the user tapped an older
#  message, or Telegram routed the callback to another instance.
#  Doing nothing there is what makes a tap look broken - so rebuild
#  the session from the button message itself, and always answer
#  with something the user can see.
# ------------------------------------------------------------

def _url_from_status_text(text: str) -> str:
    """Read the link back out of a 'Link received!' status message."""
    if not text:
        return ""
    plain = html.unescape(re.sub(r"<[^>]+>", " ", text))
    found = extract_url(plain)
    if not found or not validate_url(found):
        return ""
    return normalize_url(found)


def _recover_state(chat_id: int, call: telebot.types.CallbackQuery) -> bool:
    """Rebuild user_states[chat_id] from the text of the tapped message."""
    url = _url_from_status_text(getattr(call.message, "text", "") or "")
    if not url:
        return False

    state = {
        "url": url,
        "timestamp": datetime.now(),
        "media_type": None,
        "quality": None,
        "images": 0,
        "videos": 1,
    }
    if _is_instagram_url(url):
        # The counts only drive which buttons appear, but Back needs them.
        probe = probe_url(url)
        if probe.get("ok"):
            state["images"] = probe.get("images", 0) or 0
            state["videos"] = probe.get("videos", 1) or 1

    with _states_lock:
        user_states[chat_id] = state
    logger.info(f"Recovered session for chat {chat_id} from message text: {url}")
    return True


def _edit_or_send(chat_id: int, message_id: int, text: str,
                  markup: Optional[InlineKeyboardMarkup] = None) -> int:
    """Edit the status message; if Telegram refuses, send a new one.

    Returns the message id the next tap will carry. Swallowing the
    exception here is exactly what makes a tap look like a no-op.
    """
    try:
        bot.edit_message_text(
            text, chat_id, message_id,
            reply_markup=markup, parse_mode="HTML",
        )
        return message_id
    except Exception as exc:
        logger.warning(f"edit_message_text failed for chat {chat_id}: {exc}")
    try:
        sent = bot.send_message(
            chat_id, text, reply_markup=markup, parse_mode="HTML",
        )
        return getattr(sent, "message_id", message_id)
    except Exception as exc:
        logger.error(f"could not answer chat {chat_id}: {exc}")
        return message_id


def _session_expired(call: telebot.types.CallbackQuery) -> None:
    """Never leave a tap with neither a message nor a toast."""
    _edit_or_send(
        call.message.chat.id, call.message.message_id,
        "<b>This selection has expired.</b>\n\n"
        "Send the link again to start over.",
    )
    try:
        bot.answer_callback_query(call.id, "Session expired. Send a new link.")
    except Exception:
        pass


def _infer_media_type(quality: str) -> str:
    """Audio qualities are 128-320 kbps, video heights are 480 and up."""
    try:
        return "audio" if int(quality) < 480 else "video"
    except (TypeError, ValueError):
        return "video"


# ------------------------------------------------------------
#  TYPE SELECTION
# ------------------------------------------------------------

@bot.callback_query_handler(func=lambda call: call.data.startswith("type_"))
def handle_type_selection(call: telebot.types.CallbackQuery) -> None:
    chat_id = call.message.chat.id
    data = call.data

    # Clear the button spinner first - recovery may take a while.
    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    with _states_lock:
        have_state = chat_id in user_states
    if not have_state and not _recover_state(chat_id, call):
        _session_expired(call)
        return

    with _states_lock:
        state = user_states[chat_id]

        if data == "type_back":
            has_images = state.get("images", 0) > 0
            image_count = state.get("images", 0)
            has_video = state.get("videos", 1) > 0
            markup = _format_markup(has_images, image_count, has_video)
            text = "<b>Choose output format:</b>"
        elif data == "type_image":
            state["media_type"] = "image"
            state["quality"] = "original"
            markup = None
            text = None
        elif data == "type_video":
            state["media_type"] = "video"
            markup = _quality_markup("video")
            text = "<b>Video</b> - Select quality:"
        elif data == "type_audio":
            state["media_type"] = "audio"
            markup = _quality_markup("audio")
            text = "<b>Audio (MP3)</b> - Select quality:"
        else:
            return

    if data == "type_image":
        target = _edit_or_send(
            chat_id, call.message.message_id,
            "<b>Starting download...</b>\n\n"
            "Type: <b>Photos</b>\n\n<i>Please wait...</i>",
        )
        _start_download(chat_id, target)
        return

    _edit_or_send(chat_id, call.message.message_id, text, markup)


# ------------------------------------------------------------
#  QUALITY SELECTION -> START
# ------------------------------------------------------------

@bot.callback_query_handler(func=lambda call: call.data.startswith("q_"))
def handle_quality_selection(call: telebot.types.CallbackQuery) -> None:
    chat_id = call.message.chat.id
    quality = call.data.split("_", 1)[1]

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    with _states_lock:
        have_state = chat_id in user_states
    if not have_state and not _recover_state(chat_id, call):
        _session_expired(call)
        return

    with _states_lock:
        state = user_states[chat_id]
        state["quality"] = quality
        media_type = state.get("media_type")
        if not media_type:
            # Recovered session: a quality button is the only clue we have.
            media_type = _infer_media_type(quality)
            state["media_type"] = media_type

    quality_label = f"{quality} kbps" if media_type == "audio" else f"{quality}p"
    media_label = "Audio" if media_type == "audio" else "Video"

    target = _edit_or_send(
        chat_id, call.message.message_id,
        f"<b>Starting download...</b>\n\n"
        f"Type: <b>{media_label}</b>\n"
        f"Quality: <code>{quality_label}</code>\n\n"
        f"<i>Please wait...</i>",
    )
    _start_download(chat_id, target)


# ============================================================
#  SAFE POLLING WITH AUTO-RESTART
# ============================================================

def safe_polling() -> None:
    logger.info("Bot is starting...")
    while True:
        try:
            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=30,
                logger_level=logging.WARNING,
            )
        except KeyboardInterrupt:
            logger.info("Bot stopped by user.")
            sys.exit(0)
        except Exception as e:
            logger.error(f"Polling error: {e}", exc_info=True)
            logger.info("Restarting in 10 seconds...")
            time.sleep(10)


# ============================================================
#  HEALTH ENDPOINT
#  Railway (and any reverse proxy) can see the process is alive
#  and read the PO token state without reading logs.
# ============================================================

def start_health_server() -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    port = int(os.getenv("PORT", "8080"))

    class _Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: str) -> None:
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            path = self.path.split("?", 1)[0]
            if path in ("/", "/health", "/healthz"):
                self._send(200, json.dumps({
                    "status": "ok",
                    "yt_dlp": yt_dlp.version.__version__,
                    "bgutil_plugin": BGUTIL_VERSION,
                    "po_provider": _po_status.get("mode") or "unchecked",
                    "po_detail": _po_status.get("detail"),
                    "instagram_photos": bool(IG_PATCH_OK),
                }, ensure_ascii=False))
            else:
                self._send(404, '{"error":"not found"}')

        def log_message(self, *_args) -> None:
            pass  # keep the request log out of bot.log

    def _serve() -> None:
        try:
            httpd = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
        except OSError as exc:
            logger.warning(f"Health endpoint could not bind :{port} - {exc}")
            return
        logger.info(f"Health endpoint listening on :{port} (/, /health)")
        httpd.serve_forever()

    threading.Thread(target=_serve, daemon=True, name="health").start()


# ============================================================
#  ENTRY POINT
# ============================================================

if __name__ == "__main__":
    print("=" * 50)
    print("  Universal Media Downloader Bot")
    print("  yt-dlp + Telegram (video, audio, photos)")
    print("=" * 50)
    logger.info(f"yt-dlp version: {yt_dlp.version.__version__}")
    logger.info(f"Download directory: {DOWNLOAD_DIR}")
    logger.info(
        f"Cookie file: {COOKIE_FILE} "
        f"{'(found)' if os.path.exists(COOKIE_FILE) else '(not found)'}"
    )
    logger.info(
        f"Proxy: {'Connected' if (PROXY_ENABLED and not _proxy_needs_fallback) else 'Fallback/direct' if _proxy_needs_fallback else 'Not configured'}"
    )
    check_po_provider()
    logger.info(f"FFmpeg: {'Available' if FFMPEG_AVAILABLE else 'MISSING'}")
    logger.info(f"Max file size: {MAX_FILE_SIZE_MB} MB")
    logger.info(f"Rate limit: {MAX_REQUESTS_PER_WINDOW} req/{RATE_LIMIT_WINDOW}s")

    start_health_server()
    safe_polling()
