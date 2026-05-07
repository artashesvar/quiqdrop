# Telegram listener and main entry point
from __future__ import annotations
import asyncio
import contextlib
import html as html_lib
import json
import logging
import os
import re
import secrets
import sys
import time
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

import src.config as config
from src.reminder_scheduler import reminder_scheduler_loop
from src.db import (
    close_db,
    delete_user_config,
    evict_stale_oauth_states,
    get_reminder_preferences,
    get_user_config,
    get_workspace_name,
    init_db,
    pop_oauth_state,
    save_oauth_state,
    save_parent_page,
    save_user_token,
    update_reminder_preferences,
    update_timezone,
)
from src.notion import (
    NotionError,
    NotionOAuthError,
    NotionPageNotFoundError,
    NotionUnauthorizedError,
    app_redirect_url,
    append_url_block,
    bouncer_verify,
    close_aiohttp_session,
    create_page,
    exchange_token,
    search_pages,
    test_token,
)
from src.structure import StructuringError, structure_transcript
from src.text_cleaner import clean_transcript
from src.transcribe import transcribe_audio


def _md(text: str) -> str:
    """Escape user-controlled text for Telegram Markdown (V1) — prevents broken parsing
    when titles or names contain *, _, [, or backticks."""
    return escape_markdown(text or "", version=1)


_DISPLAY_NAME_MAX = 80


def _trim_display(text: str, limit: int = _DISPLAY_NAME_MAX) -> str:
    """Cap user-controlled display strings (workspace names, titles) so they can't
    blow past Telegram's 4096-char message limit on their own."""
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


# Common security headers for every HTML response we serve (OAuth callback + bouncer).
# Defence in depth — none of these pages host sensitive cookies, but standard headers
# are cheap insurance against bugs we haven't written yet.
_HTML_SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


def _html_response(text: str, status: int = 200, extra_headers: dict | None = None) -> web.Response:
    """Build a text/html response with our standard security headers applied."""
    headers = dict(_HTML_SECURITY_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    return web.Response(text=text, content_type="text/html", status=status, headers=headers)


# Accept http(s) URLs; reject anything else when receiving a URL to attach.
_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)

_MAX_VOICE_DURATION_SEC = 180    # 3 minutes — notes beyond this are trimmed to first 3 min
_MAX_VOICE_SIZE_BYTES = 20 * 1024 * 1024  # 20MB — Whisper hard limit is 25MB

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Suppress httpx INFO logs — they contain the full bot token in the URL
logging.getLogger("httpx").setLevel(logging.WARNING)

# Per-user asyncio.Lock so a single user can't run two voice handlers in parallel.
# We check `lock.locked()` to reject early with a friendly message instead of queueing.
# Capped indirectly by _state_sweeper_loop (FIFO eviction at 1000 users).
_user_locks: dict[int, asyncio.Lock] = {}
_USER_LOCKS_MAX = 1000


def _get_user_lock(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        # Cap before insert
        while len(_user_locks) >= _USER_LOCKS_MAX:
            oldest = next(iter(_user_locks))
            stale = _user_locks.pop(oldest, None)
            if stale and stale.locked():
                # Don't evict an in-use lock; put it back and bail out of capping.
                _user_locks[oldest] = stale
                break
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock

# Maps user_id → (note_page_id, parent_page_id) for the last successfully created note.
# parent_page_id is captured so append_url_block can verify the page actually belongs
# under the user's chosen destination before appending.
# Cleared when user starts a new voice note.
_last_note_page: dict[int, tuple[str, str]] = {}

# Maps user_id → (note_page_id, parent_page_id, expires_at_monotonic) when awaiting a URL.
# parent_page_id is carried so append_url_block can verify the page belongs to the user.
# Cleared on URL received, /cancel, timeout, or new voice note.
_pending_url: dict[int, tuple[str, str, float]] = {}
_PENDING_URL_TTL_SEC = 300  # 5 minutes

# City-labelled timezone options — maps display label to UTC offset string stored in DB.
# "UTC" (no suffix) is reserved as the "never been set" sentinel; genuine UTC users get "UTC+0".
_TIMEZONE_OPTIONS: list[tuple[str, str]] = [
    ("🌍 London (UTC+0)", "UTC+0"),
    ("🌍 Paris / Berlin (UTC+1)", "UTC+1"),
    ("🌍 Helsinki / Cairo (UTC+2)", "UTC+2"),
    ("🌍 Moscow / Nairobi (UTC+3)", "UTC+3"),
    ("🌍 Dubai (UTC+4)", "UTC+4"),
    ("🌏 Karachi (UTC+5)", "UTC+5"),
    ("🌏 Dhaka / Almaty (UTC+6)", "UTC+6"),
    ("🌏 Bangkok / Jakarta (UTC+7)", "UTC+7"),
    ("🌏 Singapore / Beijing (UTC+8)", "UTC+8"),
    ("🌏 Tokyo / Seoul (UTC+9)", "UTC+9"),
    ("🌏 Sydney (UTC+10)", "UTC+10"),
    ("🌎 New York (UTC-5)", "UTC-5"),
    ("🌎 Chicago (UTC-6)", "UTC-6"),
    ("🌎 Denver (UTC-7)", "UTC-7"),
    ("🌎 Los Angeles (UTC-8)", "UTC-8"),
]

# Page selection cache — maps short integer IDs to Notion page info per user.
# Populated on each page fetch; resets on next fetch or bot restart.
# Short IDs keep callback_data well within Telegram's 64-byte limit.
# Capped at _PAGE_CACHE_MAX_USERS — oldest user evicted (FIFO) when exceeded.
_PAGE_CACHE_MAX_USERS = 1000
_page_cache: dict[int, dict[int, dict]] = {}       # user_id → {short_id → {id, title}}

# Background sweep interval — clears expired _pending_url entries.
_STATE_SWEEP_INTERVAL_SEC = 600  # 10 minutes


async def _state_sweeper_loop() -> None:
    """Periodically drop expired _pending_url entries and stale OAuth state rows."""
    while True:
        await asyncio.sleep(_STATE_SWEEP_INTERVAL_SEC)
        try:
            now = time.monotonic()
            expired = [uid for uid, entry in _pending_url.items() if now > entry[-1]]
            for uid in expired:
                _pending_url.pop(uid, None)
            if expired:
                logger.debug("state sweeper: evicted %d expired _pending_url entries", len(expired))
            # M2: also sweep stale rows in the pending_oauth DB table.
            await evict_stale_oauth_states()
        except Exception as exc:
            logger.error("state sweeper failed: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_oauth_url(state: str) -> str:
    """Build the Notion OAuth authorisation URL. owner=user required for public integrations."""
    params = urlencode({
        "client_id": config.NOTION_CLIENT_ID,
        "response_type": "code",
        "owner": "user",
        "redirect_uri": config.NOTION_REDIRECT_URI,
        "state": state,
    })
    return f"https://api.notion.com/v1/oauth/authorize?{params}"


def _build_parent_keyboard(user_id: int, pages: list[dict]) -> InlineKeyboardMarkup:
    """
    Shows top-level pages only. Tapping a page saves it immediately as the destination.
    Assigns short integer IDs and caches them to stay within Telegram's 64-byte
    callback_data limit. Format: page_parent:{short_id} (~14 bytes).
    """
    # Cap _page_cache size — drop oldest entries (FIFO) when exceeded.
    while len(_page_cache) >= _PAGE_CACHE_MAX_USERS:
        oldest = next(iter(_page_cache))
        _page_cache.pop(oldest, None)
    _page_cache[user_id] = {}
    buttons = []
    i = 0
    for p in pages:
        if not p.get("is_top_level"):
            continue
        _page_cache[user_id][i] = {"id": p["id"], "title": p["title"]}
        buttons.append([InlineKeyboardButton(p["title"][:40], callback_data=f"page_parent:{i}")])
        i += 1
    return InlineKeyboardMarkup(buttons)


_NO_TOP_LEVEL_MSG = (
    "No top-level pages found. All your shared pages appear to be subpages.\n\n"
    "In Notion, share a top-level page with QuiqDrop, then try again."
)


def _build_timezone_keyboard(pending_toggle: str) -> InlineKeyboardMarkup:
    """
    Build the timezone picker keyboard.
    pending_toggle: 'daily' or 'weekly' — encoded in callback_data so we know
    which reminder to enable after the user picks their timezone.
    callback_data format: tz_select:{tz_value}:{pending_toggle}
    Longest value: "tz_select:UTC+10:weekly" = 22 bytes (well within 64-byte limit).
    """
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"tz_select:{tz}:{pending_toggle}")]
        for label, tz in _TIMEZONE_OPTIONS
    ]
    return InlineKeyboardMarkup(buttons)


def _truncate_url(url: str, max_len: int = 45) -> str:
    """Truncate a URL to max_len chars, appending '...' if cut."""
    return url[:max_len] + "..." if len(url) > max_len else url


_NOTION_WEB_PREFIX = "https://www.notion.so/"


def _js_str(value: str) -> str:
    """JSON-encode a string for safe embedding inside a <script> tag.

    json.dumps does NOT escape the sequence </script>, which HTML parsers treat
    as the end of the script tag even inside a string literal. This is the
    standard mitigation: replace </ with <\\/ so the parser never sees </script>.
    """
    return json.dumps(value).replace("</", r"<\/")


_APP_REDIRECT_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Opening in Notion…</title>
<style>
  body {{ font-family: -apple-system, system-ui, Segoe UI, Roboto, sans-serif;
         padding: 2rem; color: #333; text-align: center; }}
  a {{ color: #2383e2; }}
</style>
</head><body>
<p>Opening your note in the Notion app…</p>
<p><a href="{web_href}">Tap here if it didn't open</a></p>
<script>
  var appUrl = {app_json};
  var webUrl = {web_json};
  // Cancel the web fallback if the OS handled the notion:// scheme —
  // the page becomes hidden/unloaded when the Notion app takes over.
  var timer = setTimeout(function() {{ window.location.replace(webUrl); }}, 1500);
  document.addEventListener('visibilitychange', function() {{
    if (document.hidden) {{ clearTimeout(timer); }}
  }});
  window.addEventListener('pagehide', function() {{ clearTimeout(timer); }});
  window.location.replace(appUrl);
</script>
</body></html>"""


async def _app_redirect_handler(request: web.Request) -> web.Response:
    """Serve the deep-link bouncer page for /r/{path}?sig=…&...

    Verifies the HMAC signature: only paths we generated via app_redirect_url
    are accepted. Closes the open-redirector hole on our domain — an attacker
    can't craft /r/login-… URLs that bounce to a real Notion login.
    """
    path = request.match_info.get("path", "")
    # Pull sig out and rebuild the canonical query that was originally signed.
    query_pairs = [(k, v) for k, v in request.rel_url.query.items() if k != "sig"]
    sig = request.rel_url.query.get("sig", "")
    canonical_query = urlencode(query_pairs)

    if not bouncer_verify(path, canonical_query, sig):
        logger.warning("Bouncer: signature verification failed for path=%.200s", path)
        return _html_response("Invalid or expired Notion link.", status=400)

    suffix = f"?{canonical_query}" if canonical_query else ""
    notion_web = f"{_NOTION_WEB_PREFIX}{path}{suffix}"
    notion_app = f"notion://www.notion.so/{path}{suffix}"
    logger.debug("Bouncer: serving redirect for path=%.80s", path)
    body = _APP_REDIRECT_HTML.format(
        web_href=html_lib.escape(notion_web, quote=True),
        web_json=_js_str(notion_web),
        app_json=_js_str(notion_app),
    )
    # CSP allows our inline script + style only, no external loads.
    return _html_response(body, extra_headers={
        "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; img-src 'none'",
    })


def _default_structured(transcript: str) -> dict:
    """The minimal structured payload we use when AI structuring is disabled or fails."""
    return {"title": "Voice note", "summary": transcript, "key_points": []}


_DOWNLOAD_TIMEOUT_SEC = 60  # Cap on Telegram CDN download — prevents the handler hanging forever


async def _safe_remove(path: str | None) -> None:
    """Best-effort file delete: suppress missing-file errors, run in a thread to avoid
    blocking the event loop, log at DEBUG so cleanup doesn't spam INFO."""
    if not path:
        return
    def _do_remove(p: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.remove(p)
    try:
        await asyncio.to_thread(_do_remove, path)
        logger.debug("Cleaned up %s", path)
    except Exception as exc:
        logger.warning("Failed to remove %s: %s", path, exc)


def _do_trim(src: str, dst: str, max_ms: int) -> None:
    """Synchronous pydub trim — called via asyncio.to_thread."""
    from pydub import AudioSegment
    audio = AudioSegment.from_file(src)
    audio[:max_ms].export(dst, format="ogg")


async def _trim_audio(src_path: str, max_sec: int) -> str:
    """Trim audio file to max_sec seconds. Returns path to trimmed file."""
    dst_path = src_path.replace(".ogg", "_trimmed.ogg")
    await asyncio.to_thread(_do_trim, src_path, dst_path, max_sec * 1000)
    logger.info("Trimmed audio from %s to %ds → %s", src_path, max_sec, dst_path)
    return dst_path


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — greet user and show connection status."""
    user = update.effective_user
    logger.info("User %s sent /start", user.id)

    config_row = await get_user_config(user.id)
    if config_row is not None:
        _, page_id, page_title = config_row
        page_status = f"*{_md(_trim_display(page_title))}*" if page_title else ("selected ✅" if page_id else "not selected yet — use /settings")
        await update.message.reply_text(
            f"Welcome back, {_md(user.first_name)}! 👋\n\n"
            f"Your Notion workspace is connected.\n"
            f"Destination page: {page_status}\n\n"
            "Send me a voice note anytime 🎤",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"Hey {_md(user.first_name)}! 👋\n\n"
            "I'm *QuiqDrop* — your voice-to-Notion assistant.\n\n"
            "To get started, connect your Notion workspace:\n"
            "👉 /connect",
            parse_mode="Markdown",
        )


async def connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /connect — send the Notion OAuth URL."""
    user = update.effective_user
    logger.info("User %s initiated /connect", user.id)

    await evict_stale_oauth_states()
    state = secrets.token_hex(16)
    await save_oauth_state(state, user.id)

    url = _build_oauth_url(state)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Connect to Notion", url=url)]
    ])
    await update.message.reply_text(
        "Click below to connect your Notion workspace:\n\n"
        "📱 *Android users:* if the button opens the Notion app instead of the auth page, "
        "go to your phone's Settings → Apps → Notion → \"Open by default\" → clear defaults. "
        "Then try again.",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /settings — show current config with Change Page / Disconnect buttons."""
    user = update.effective_user
    config_row = await get_user_config(user.id)
    if config_row is None:
        await update.message.reply_text(
            "You haven't connected Notion yet. Use /connect to get started."
        )
        return

    _, page_id, page_title = config_row
    workspace = _trim_display(await get_workspace_name(user.id) or "Notion")
    page_display = f"*{_md(_trim_display(page_title))}*" if page_title else ("Selected ✅" if page_id else "_not selected yet_")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Disconnect", callback_data="disconnect_confirm_prompt")],
        [InlineKeyboardButton("⏰ Reminders", callback_data="settings_reminders")],
    ])
    await update.message.reply_text(
        f"*Your Notion settings*\n\nWorkspace: {_md(workspace)}\nDestination page: {page_display}",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /disconnect — ask for confirmation."""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Yes, disconnect", callback_data="disconnect_confirm"),
            InlineKeyboardButton("Cancel", callback_data="disconnect_cancel"),
        ]
    ])
    await update.message.reply_text(
        "Are you sure you want to disconnect your Notion workspace?",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Reminder settings helpers
# ---------------------------------------------------------------------------

async def _build_reminder_settings(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Build (text, InlineKeyboardMarkup) for the reminder settings submenu."""
    prefs = await get_reminder_preferences(user_id)
    _, daily, weekly = prefs if prefs else ("UTC", True, True)
    daily_status = "✅ Enabled" if daily else "❌ Disabled"
    weekly_status = "✅ Enabled" if weekly else "❌ Disabled"
    text = (
        "⏰ *Reminder Settings*\n\n"
        f"Daily reminders (9am): {daily_status}\n"
        f"Weekly reminders (Monday 9am): {weekly_status}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"Daily: {'ON ✅' if daily else 'OFF ❌'}",
            callback_data="toggle_daily",
        )],
        [InlineKeyboardButton(
            f"Weekly: {'ON ✅' if weekly else 'OFF ❌'}",
            callback_data="toggle_weekly",
        )],
        [InlineKeyboardButton("« Back", callback_data="settings_back")],
    ])
    return text, keyboard


# ---------------------------------------------------------------------------
# Callback query handler (inline keyboard buttons)
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all inline keyboard button presses."""
    query = update.callback_query
    # Must always be called — dismisses the Telegram loading spinner on the button
    await query.answer()

    user = query.from_user
    data = query.data

    if data.startswith("page_parent:"):
        # User tapped a top-level page — save it directly as the destination.
        short_id = int(data.split(":")[1])
        cached = _page_cache.get(user.id, {}).get(short_id)
        if not cached:
            await query.edit_message_text("Session expired. Use /settings to pick a page again.")
            return
        page_id, page_title = cached["id"], cached["title"]
        await save_parent_page(user.id, page_id, page_title)
        logger.info("User %s selected page %s (%s)", user.id, page_id, page_title)
        await query.edit_message_text(
            f"✅ All set! Your notes will be saved to 👉 *{_md(_trim_display(page_title))}*\n\n"
            "Send me a voice note anytime 🎤",
            parse_mode="Markdown",
        )

    elif data == "disconnect_confirm_prompt":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Yes, disconnect", callback_data="disconnect_confirm"),
                InlineKeyboardButton("Cancel", callback_data="disconnect_cancel"),
            ]
        ])
        await query.edit_message_text("Are you sure?", reply_markup=keyboard)

    elif data == "disconnect_confirm":
        await delete_user_config(user.id)
        logger.info("User %s disconnected", user.id)
        await query.edit_message_text(
            "Disconnected. Use /connect to reconnect anytime."
        )

    elif data == "disconnect_cancel":
        await query.edit_message_text("Cancelled. Your workspace is still connected.")

    elif data == "settings_reminders":
        text, keyboard = await _build_reminder_settings(user.id)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

    elif data == "toggle_daily":
        prefs = await get_reminder_preferences(user.id)
        tz, daily, _ = prefs if prefs else ("UTC", False, False)
        if not daily and tz == "UTC":
            # Toggling ON for the first time and timezone never set — ask for timezone first
            await query.edit_message_text(
                "⏰ Before enabling reminders, let me know your timezone so I can send them at 9am your time:",
                reply_markup=_build_timezone_keyboard("daily"),
            )
        else:
            await update_reminder_preferences(user.id, daily_enabled=not daily)
            text, keyboard = await _build_reminder_settings(user.id)
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

    elif data == "toggle_weekly":
        prefs = await get_reminder_preferences(user.id)
        tz, _, weekly = prefs if prefs else ("UTC", False, False)
        if not weekly and tz == "UTC":
            # Toggling ON for the first time and timezone never set — ask for timezone first
            await query.edit_message_text(
                "⏰ Before enabling reminders, let me know your timezone so I can send them at 9am your time:",
                reply_markup=_build_timezone_keyboard("weekly"),
            )
        else:
            await update_reminder_preferences(user.id, weekly_enabled=not weekly)
            text, keyboard = await _build_reminder_settings(user.id)
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

    elif data == "settings_back":
        config_row = await get_user_config(user.id)
        if config_row is None:
            await query.edit_message_text("No Notion workspace connected. Use /connect.")
            return
        _, page_id, page_title = config_row
        workspace = await get_workspace_name(user.id) or "Notion"
        page_display = f"*{_md(page_title)}*" if page_title else ("Selected ✅" if page_id else "_not selected yet_")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Disconnect", callback_data="disconnect_confirm_prompt")],
            [InlineKeyboardButton("⏰ Reminders", callback_data="settings_reminders")],
        ])
        await query.edit_message_text(
            f"*Your Notion settings*\n\nWorkspace: {_md(workspace)}\nDestination page: {page_display}",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    elif data.startswith("tz_select:"):
        _, tz_value, pending_toggle = data.split(":", 2)
        await update_timezone(user.id, tz_value)
        if pending_toggle == "daily":
            await update_reminder_preferences(user.id, daily_enabled=True)
        else:
            await update_reminder_preferences(user.id, weekly_enabled=True)
        text, keyboard = await _build_reminder_settings(user.id)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

    elif data == "add_url":
        last = _last_note_page.get(user.id)
        if not last:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                "This note is too old to attach a URL."
            )
            return
        page_id, parent_page_id = last
        _pending_url[user.id] = (page_id, parent_page_id, time.monotonic() + _PENDING_URL_TTL_SEC)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "Send me the URL to add to your note, or /cancel to stop."
        )

    else:
        logger.warning("handle_callback: unrecognised callback_data=%s from user %s", data, user.id)


# ---------------------------------------------------------------------------
# Voice message handler
# ---------------------------------------------------------------------------

async def _resolve_user_config(
    update: Update, user_id: int
) -> tuple[str, str] | None:
    """Auth check. Returns (token, parent_page_id) or replies and returns None."""
    config_row = await get_user_config(user_id)
    if config_row is None:
        await update.message.reply_text(
            "You need to connect your Notion workspace first.\nUse /connect to get started."
        )
        return None
    token, parent_page_id, _ = config_row
    if not parent_page_id:
        await update.message.reply_text(
            "You haven't selected a destination page yet.\nUse /settings to choose one."
        )
        return None
    return token, parent_page_id


async def _download_voice_file(update: Update, file_path: str) -> bool:
    """Download voice to file_path with a timeout. Replies and returns False on
    too-large or timeout. True on success."""
    voice_file = await update.message.voice.get_file()
    try:
        await asyncio.wait_for(
            voice_file.download_to_drive(file_path),
            timeout=_DOWNLOAD_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        logger.error("Voice download timed out for %s after %ds", file_path, _DOWNLOAD_TIMEOUT_SEC)
        with contextlib.suppress(Exception):
            await update.message.reply_text(
                "📡 Couldn't pull your audio from Telegram in time. Please try again."
            )
        return False

    file_size = await asyncio.to_thread(os.path.getsize, file_path)
    logger.info("Downloaded to %s | size=%d bytes", file_path, file_size)
    if file_size > _MAX_VOICE_SIZE_BYTES:
        await update.message.reply_text(
            "Voice note file is too large. Please send a shorter recording."
        )
        return False
    return True


async def _transcribe_with_progress(update: Update, user_id: int, audio_path: str) -> str:
    """Send the 'hang tight' message + 10s gem nudge, then transcribe. Returns the
    raw transcript string (may be empty)."""
    progress_msg = await update.message.reply_text(
        "Turning your thoughts into text... ✍️ hang tight!"
    )

    async def _gem_nudge(msg: Message) -> None:
        await asyncio.sleep(10)
        try:
            await msg.edit_text(
                "Turning your thoughts into text... ✍️ hang tight!\n\n"
                "Wait, you're dropping gems! 💎 Need a few more seconds. "
                "Don't want to miss a single one."
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("gem_nudge edit failed for user %s: %s", user_id, exc)

    gem_task = asyncio.create_task(_gem_nudge(progress_msg))
    try:
        return await transcribe_audio(audio_path)
    finally:
        gem_task.cancel()


async def _structure_or_fallback(update: Update, transcript: str) -> dict:
    """Apply optional cleaning + AI structuring. Returns a structured dict — never raises."""
    if config.ENABLE_TRANSCRIPT_CLEANING:
        before = len(transcript)
        transcript = clean_transcript(transcript)
        logger.info("Cleaning: %d→%d chars", before, len(transcript))

    if not config.ENABLE_AI_STRUCTURING:
        return _default_structured(transcript)

    await update.message.reply_text("Extracting the key points for you.")
    try:
        return await structure_transcript(transcript)
    except StructuringError as e:
        logger.warning("Structuring failed, falling back to plain transcript: %s", e)
        return _default_structured(transcript)


async def _save_and_reply(
    update: Update,
    user_id: int,
    token: str,
    parent_page_id: str,
    structured: dict,
    transcript: str,
    is_trimmed: bool,
) -> None:
    """Send 'sending to Notion', save the page, and reply with the final result.
    Handles all Notion errors with user-visible messages."""
    await update.message.reply_text("Sending this straight to your Notion... 🧠")
    title_md = _md(structured.get("title") or "Voice note")
    summary_md = _md(structured.get("summary") or "")

    try:
        page_id, page_url = await create_page(token, parent_page_id, structured, transcript)
    except NotionUnauthorizedError:
        logger.warning("Notion token expired for user %s", user_id)
        await update.message.reply_text(
            f"📄 {title_md}\n\n{summary_md}\n\n"
            "⚠️ Notion connection expired. Use /connect to reconnect.",
            parse_mode="Markdown",
        )
        return
    except NotionPageNotFoundError:
        logger.warning("Parent page deleted for user %s — clearing stored page", user_id)
        await save_parent_page(user_id, None)
        await update.message.reply_text(
            f"📄 {title_md}\n\n{summary_md}\n\n"
            "⚠️ The destination page no longer exists in Notion. Use /settings to pick a new one.",
            parse_mode="Markdown",
        )
        return
    except NotionError as e:
        logger.error("Notion save failed for user %s: %s", user_id, e)
        await update.message.reply_text(
            f"📄 {title_md}\n\n{summary_md}\n\n"
            "⚠️ Transcribed but couldn't save to Notion. Please try again.",
            parse_mode="Markdown",
        )
        return

    _last_note_page[user_id] = (page_id, parent_page_id)
    saved_line = "✅ Saved to Notion - only the first 3 min!" if is_trimmed else "✅ Saved to Notion!"
    link_href = page_url
    await update.message.reply_text(
        f"{saved_line}\n\n"
        f"📄 {title_md}\n\n"
        f"{summary_md}\n\n"
        f"[Open in Notion ↗]({link_href})",
        parse_mode="Markdown",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔗 Add URL", callback_data="add_url")],
        ]),
    )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Orchestrate: auth check → download → trim → transcribe → structure → save."""
    user = update.effective_user
    duration = update.message.voice.duration
    file_unique_id = update.message.voice.file_unique_id
    logger.info("Voice from user %s | duration=%ds", user.id, duration)

    if duration < 3:
        await update.message.reply_text("That voice clip was a little too short! ⚠️")
        return

    lock = _get_user_lock(user.id)
    if lock.locked():
        await update.message.reply_text(
            "⏳ Your previous recording is still processing, please wait."
        )
        return

    # Clear any prior attachment state — new voice note supersedes it
    _last_note_page.pop(user.id, None)
    _pending_url.pop(user.id, None)

    # Random suffix on the /tmp path so two requests with the same file_unique_id
    # (rare but possible if Telegram reuses one) can't collide on disk.
    file_path = f"/tmp/voice_{user.id}_{file_unique_id}_{secrets.token_hex(4)}.ogg"
    trimmed_path: str | None = None

    async with lock:
        try:
            resolved = await _resolve_user_config(update, user.id)
            if resolved is None:
                return
            token, parent_page_id = resolved

            if not await _download_voice_file(update, file_path):
                return

            # M14: announce trim AFTER we have the file in hand, so we never promise
            # a trim that won't happen because of a download failure.
            is_trimmed = duration > _MAX_VOICE_DURATION_SEC
            process_path = file_path
            if is_trimmed:
                await update.message.reply_text(
                    "⚠️ Your note is over 3 min — I'll process the first 3 min only..."
                )
                trimmed_path = await _trim_audio(file_path, _MAX_VOICE_DURATION_SEC)
                process_path = trimmed_path

            transcript = await _transcribe_with_progress(update, user.id, process_path)
            if not transcript.strip():
                await update.message.reply_text(
                    "I couldn't hear anything clearly — please try again."
                )
                return

            structured = await _structure_or_fallback(update, transcript)
            await _save_and_reply(
                update, user.id, token, parent_page_id, structured, transcript, is_trimmed
            )

        except asyncio.TimeoutError:
            logger.error("Voice processing timed out for user %s", user.id)
            with contextlib.suppress(Exception):
                await update.message.reply_text(
                    "⏳ This took longer than expected. Please try again — a shorter note usually helps."
                )
        except (TimedOut, NetworkError) as e:
            logger.error("Telegram network error for user %s: %s", user.id, e)
            with contextlib.suppress(Exception):
                await update.message.reply_text(
                    "📡 Network hiccup talking to Telegram. Please try sending the voice note again."
                )
        except BadRequest as e:
            logger.error("Telegram bad request for user %s: %s", user.id, e)
            with contextlib.suppress(Exception):
                await update.message.reply_text(
                    "⚠️ Telegram couldn't process that message. Please try again."
                )
        except OSError as e:
            logger.error("File I/O error for user %s: %s", user.id, e)
            with contextlib.suppress(Exception):
                await update.message.reply_text(
                    "⚠️ Couldn't read or save the audio on our side. Please try again."
                )
        except Exception as e:
            logger.error("handle_voice failed for user %s: %s", user.id, e, exc_info=True)
            with contextlib.suppress(Exception):
                await update.message.reply_text("Something went wrong. Please try again.")
        finally:
            await _safe_remove(file_path)
            await _safe_remove(trimmed_path)


# ---------------------------------------------------------------------------
# URL attachment handlers
# ---------------------------------------------------------------------------

async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /cancel — exit the URL attachment waiting state."""
    user = update.effective_user
    if user.id in _pending_url:
        _pending_url.pop(user.id)
        await update.message.reply_text("Cancelled.")
    else:
        await update.message.reply_text("Nothing to cancel.")


async def handle_pending_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text messages — used to receive URLs when in attachment waiting state."""
    user = update.effective_user
    if user.id not in _pending_url:
        # Not in attachment state — fall through to handle_unknown
        await handle_unknown(update, context)
        return

    page_id, parent_page_id, expires_at = _pending_url[user.id]
    if time.monotonic() > expires_at:
        _pending_url.pop(user.id, None)
        await update.message.reply_text(
            "⏳ Your attachment window expired. Send a voice note and try again."
        )
        return

    url_text = update.message.text.strip()

    # Reject anything that doesn't look like a URL — the user can try again or /cancel
    if not _URL_RE.match(url_text) or len(url_text) > 2000:
        await update.message.reply_text(
            "That doesn't look like a URL. Send a link starting with http:// or https://, or /cancel to stop."
        )
        return

    _pending_url.pop(user.id)  # consume immediately — prevent double-submit

    config_row = await get_user_config(user.id)
    if config_row is None:
        await update.message.reply_text(
            "You need to connect your Notion workspace first.\nUse /connect to get started."
        )
        return
    token, _, _2 = config_row

    try:
        await append_url_block(token, page_id, url_text, expected_parent_id=parent_page_id)
        await update.message.reply_text("✅ URL added to your note!")
    except NotionUnauthorizedError:
        await update.message.reply_text(
            "⚠️ Notion connection expired. Use /connect to reconnect."
        )
    except NotionPageNotFoundError:
        logger.warning("append_url_block: page missing or parent mismatch for user %s", user.id)
        await update.message.reply_text(
            "⚠️ Couldn't find that note in Notion anymore. Send a new voice note to start over."
        )
    except NotionError as e:
        logger.error("append_url_block failed for user %s: %s", user.id, e)
        await update.message.reply_text(
            "⚠️ Couldn't add the URL. Please try again."
        )


# ---------------------------------------------------------------------------
# Fallback and error handlers
# ---------------------------------------------------------------------------

async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle any non-voice, non-command message."""
    if not update.message:
        return
    await update.message.reply_text("Send me a voice note to get started 🎤")


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log all unhandled handler errors so nothing fails silently."""
    logger.error("Update %s caused error: %s", update, context.error, exc_info=context.error)


# ---------------------------------------------------------------------------
# OAuth callback web server
# ---------------------------------------------------------------------------

async def _oauth_callback(request: web.Request, ptb_app: Application) -> web.Response:
    """Handle GET /oauth/notion/callback from Notion after user authorises."""
    try:
        return await _oauth_callback_inner(request, ptb_app)
    except Exception:
        logger.exception("Unexpected error in OAuth callback")
        return _html_response(
            "<html><body><h1>Something went wrong.</h1>"
            "<p>Please return to Telegram and try /connect again.</p></body></html>",
            status=500,
        )


async def _oauth_callback_inner(request: web.Request, ptb_app: Application) -> web.Response:
    """Core OAuth callback logic — called by _oauth_callback which wraps it in a catch-all."""
    code = request.rel_url.query.get("code")
    state = request.rel_url.query.get("state")
    error = request.rel_url.query.get("error")

    # User clicked "Cancel" or denied access in Notion — Notion sends error=access_denied
    # plus the original state so we can identify the user and notify them in Telegram.
    if error:
        logger.info("OAuth callback received error=%s state=%s", error, state)
        if state:
            cancel_result = await pop_oauth_state(state)
            if cancel_result.user_id is not None and cancel_result.status in ("ok", "expired"):
                with contextlib.suppress(Exception):
                    await ptb_app.bot.send_message(
                        chat_id=cancel_result.user_id,
                        text=(
                            "Notion connection was cancelled. "
                            "When you're ready, use /connect to try again."
                        ),
                    )
        return _html_response(
            "<html><body><h1>Connection cancelled.</h1>"
            "<p>You can return to Telegram and try /connect again whenever you're ready.</p></body></html>",
        )

    if not code or not state:
        return _html_response(
            "<html><body><h1>Invalid request — missing code or state.</h1></body></html>",
            status=400,
        )

    state_result = await pop_oauth_state(state)
    user_id = state_result.user_id
    if state_result.status == "unknown":
        logger.warning("OAuth callback received unknown state: %s", state)
        return _html_response(
            "<html><body><h1>Unknown session.</h1>"
            "<p>Please return to Telegram and use /connect again.</p></body></html>",
            status=400,
        )

    if state_result.status == "consumed":
        # Browser retried, double-clicked, or shared the URL across devices.
        # The first hit already completed the connection. Show a friendly success page
        # without re-running the token exchange.
        logger.info("OAuth callback retry on consumed state for user %s — idempotent ack", user_id)
        return _html_response(
            "<html><body><h1>Already connected!</h1>"
            "<p>Your Notion connection is set up — return to Telegram to start saving voice notes.</p>"
            "</body></html>",
        )

    if state_result.status == "expired":
        logger.warning("OAuth state expired for user %s", user_id)
        with contextlib.suppress(Exception):
            await ptb_app.bot.send_message(
                chat_id=user_id,
                text=(
                    "⏳ Your connection link expired. "
                    "Use /connect to start fresh — it only takes a moment."
                ),
            )
        return _html_response(
            "<html><body><h1>This link expired.</h1>"
            "<p>Return to Telegram and use /connect again to get a fresh link.</p></body></html>",
            status=400,
        )

    # state_result.status == "ok" — fresh, valid, just marked consumed

    # From here we know user_id — wrap the rest so any unexpected exception still
    # produces a user-visible Telegram message (M13).
    try:
        try:
            access_token, workspace_name = await exchange_token(code)
        except NotionOAuthError as e:
            logger.error("Token exchange failed for user %s: %s", user_id, e)
            with contextlib.suppress(Exception):
                await ptb_app.bot.send_message(
                    chat_id=user_id,
                    text="❌ Couldn't complete the Notion connection. Please try /connect again.",
                )
            return _html_response(
                "<html><body><h1>Failed to connect to Notion.</h1>"
                "<p>Return to Telegram and try /connect again.</p></body></html>",
                status=500,
            )

        # Verify the token is actually usable BEFORE persisting it. A 200 with a bad
        # token would otherwise quietly land in the DB and fail at first voice note.
        try:
            token_ok = await test_token(access_token)
        except NotionError as e:
            logger.error("test_token errored for user %s: %s", user_id, e)
            token_ok = False
        if not token_ok:
            logger.error("Token from Notion failed test_token for user %s", user_id)
            with contextlib.suppress(Exception):
                await ptb_app.bot.send_message(
                    chat_id=user_id,
                    text="❌ Notion accepted the connection but the access token didn't work. Please try /connect again.",
                )
            return _html_response(
                "<html><body><h1>Connection invalid.</h1>"
                "<p>Return to Telegram and try /connect again.</p></body></html>",
                status=500,
            )

        await save_user_token(user_id, access_token, workspace_name)
        logger.info("OAuth complete for user %s (workspace: %s)", user_id, workspace_name)

        # Distinguish "API call failed" from "API succeeded with zero pages" (H1).
        search_failed = False
        try:
            pages = await search_pages(access_token)
        except NotionError as e:
            logger.error("search_pages failed after OAuth for user %s: %s", user_id, e)
            pages = []
            search_failed = True

        workspace_md = _md(_trim_display(workspace_name))
        if search_failed:
            await ptb_app.bot.send_message(
                chat_id=user_id,
                text=(
                    f"Connected to *{workspace_md}* 🎉\n\n"
                    "But I couldn't reach the Notion API to load your pages just now. "
                    "Open /settings in a moment and try again — your connection is saved."
                ),
                parse_mode="Markdown",
            )
        elif pages:
            keyboard = _build_parent_keyboard(user_id, pages)
            top_level = keyboard.inline_keyboard
            if len(top_level) == 1:
                # Only one page shared — auto-select it, no picker needed.
                short_id = int(top_level[0][0].callback_data.split(":")[1])
                cached = _page_cache.get(user_id, {}).get(short_id)
                if cached:
                    await save_parent_page(user_id, cached["id"], cached["title"])
                    await ptb_app.bot.send_message(
                        chat_id=user_id,
                        text=(
                            f"Connected to *{workspace_md}* 🎉\n\n"
                            f"Your notes will be saved to 👉 *{_md(_trim_display(cached['title']))}*\n\n"
                            "Send me a voice note anytime 🎤"
                        ),
                        parse_mode="Markdown",
                    )
                else:
                    # Cache miss — fall back to picker
                    await ptb_app.bot.send_message(
                        chat_id=user_id,
                        text=f"Connected to *{workspace_md}* 🎉\n\nNow choose where to save your notes:",
                        parse_mode="Markdown",
                        reply_markup=keyboard,
                    )
            elif top_level:
                await ptb_app.bot.send_message(
                    chat_id=user_id,
                    text=f"Connected to *{workspace_md}* 🎉\n\nYou shared more than 1 page. Pick the one as your final destination.",
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
            else:
                await ptb_app.bot.send_message(
                    chat_id=user_id,
                    text=f"Connected to *{workspace_md}* 🎉\n\n{_NO_TOP_LEVEL_MSG}",
                    parse_mode="Markdown",
                )
        else:
            await ptb_app.bot.send_message(
                chat_id=user_id,
                text=(
                    f"Connected to *{workspace_md}* 🎉\n\n"
                    "No pages found yet. Share a page with the QuiqDrop integration in Notion, "
                    "then use /settings to select it."
                ),
                parse_mode="Markdown",
            )

        return _html_response(
            "<html><body>"
            "<h1>Connected to Notion!</h1>"
            "<p>You can close this tab and return to Telegram.</p>"
            "</body></html>",
        )
    except Exception:
        logger.exception("Unexpected error in OAuth callback for user %s", user_id)
        with contextlib.suppress(Exception):
            await ptb_app.bot.send_message(
                chat_id=user_id,
                text="❌ Something went wrong while completing your connection. Please try /connect again.",
            )
        raise


def _create_web_app(ptb_app: Application) -> web.Application:
    """Create the aiohttp web application for OAuth callbacks and health checks."""
    app = web.Application()

    async def callback_handler(request: web.Request) -> web.Response:
        return await _oauth_callback(request, ptb_app)

    async def health_handler(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/oauth/notion/callback", callback_handler)
    app.router.add_get("/health", health_handler)
    # Deep-link bouncer — opens notion:// on mobile, falls back to https on desktop.
    app.router.add_get("/r/{path:.*}", _app_redirect_handler)
    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _run() -> None:
    await init_db()

    ptb_app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .build()
    )

    ptb_app.add_handler(CommandHandler("start", start))
    ptb_app.add_handler(CommandHandler("connect", connect))
    ptb_app.add_handler(CommandHandler("settings", settings))
    ptb_app.add_handler(CommandHandler("disconnect", disconnect))
    ptb_app.add_handler(CommandHandler("cancel", handle_cancel))
    ptb_app.add_handler(CallbackQueryHandler(handle_callback))
    ptb_app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    ptb_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pending_url))
    ptb_app.add_handler(MessageHandler(filters.ALL, handle_unknown))
    ptb_app.add_error_handler(handle_error)

    runner = web.AppRunner(_create_web_app(ptb_app))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.PORT).start()
    logger.info("Web server started on port %d", config.PORT)

    async with ptb_app:
        await ptb_app.start()
        await ptb_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("Bot polling started.")
        scheduler_task = asyncio.create_task(reminder_scheduler_loop(ptb_app.bot))
        sweeper_task = asyncio.create_task(_state_sweeper_loop())
        try:
            await asyncio.Event().wait()  # run until Ctrl+C / SIGTERM
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            scheduler_task.cancel()
            sweeper_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper_task
            # Must stop before async with __aexit__ calls shutdown() — otherwise RuntimeError
            await ptb_app.updater.stop()
            await ptb_app.stop()

    await runner.cleanup()
    await close_aiohttp_session()
    await close_db()
    logger.info("Shutdown complete.")


def main() -> None:
    logger.info("Starting QuiqDrop bot...")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
