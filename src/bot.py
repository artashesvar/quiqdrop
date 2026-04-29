# Telegram listener and main entry point
import asyncio
import contextlib
import logging
import os
import secrets
import sys
import time
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import src.config as config
from src.reminder_scheduler import reminder_scheduler_loop
from src.db import (
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
    append_url_block,
    create_page,
    exchange_token,
    search_pages,
)
from src.structure import StructuringError, structure_transcript
from src.text_cleaner import clean_transcript
from src.transcribe import transcribe_audio

_MAX_VOICE_DURATION_SEC = 180    # 3 minutes — notes beyond this are trimmed to first 3 min
_MAX_VOICE_SIZE_BYTES = 20 * 1024 * 1024  # 20MB — Whisper hard limit is 25MB

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Suppress httpx INFO logs — they contain the full bot token in the URL
logging.getLogger("httpx").setLevel(logging.WARNING)

# Tracks users currently processing a voice note — prevents concurrent API hammering.
# Removed from the set in handle_voice's finally block.
_processing_users: set[int] = set()

# Maps user_id → page_id of the last successfully created note.
# Cleared when user starts a new voice note.
_last_note_page: dict[int, str] = {}

# Maps user_id → (page_id, expires_at_monotonic) when awaiting a URL to attach.
# Cleared on URL received, /cancel, timeout, or new voice note.
_pending_url: dict[int, tuple[str, float]] = {}
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
# Grows by ~1 entry per user per page-fetch session — negligible at MVP scale.
# If user count grows significantly, add eviction (e.g. keep only last N users).
_page_cache: dict[int, dict[int, dict]] = {}       # user_id → {short_id → {id, title}}


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


def _format_structured(data: dict) -> str:
    """Format a structured dict into a readable Telegram message."""
    parts = [f"📝 {data['title']}", "", data["summary"]]

    if data.get("key_points"):
        parts.append("")
        parts.append("💡 Key Points:")
        for point in data["key_points"]:
            parts.append(f"• {point}")

    if data.get("action_items"):
        parts.append("")
        parts.append("✅ Action Items:")
        for item in data["action_items"]:
            parts.append(f"• {item}")

    if data.get("decisions"):
        parts.append("")
        parts.append("🧠 Decisions:")
        for decision in data["decisions"]:
            parts.append(f"• {decision}")

    return "\n".join(parts)


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
        page_status = f"*{page_title}*" if page_title else ("selected ✅" if page_id else "not selected yet — use /settings")
        await update.message.reply_text(
            f"Welcome back, {user.first_name}! 👋\n\n"
            f"Your Notion workspace is connected.\n"
            f"Destination page: {page_status}\n\n"
            "Send me a voice note anytime 🎤",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"Hey {user.first_name}! 👋\n\n"
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
    workspace = await get_workspace_name(user.id) or "Notion"
    page_display = f"*{page_title}*" if page_title else ("Selected ✅" if page_id else "_not selected yet_")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Disconnect", callback_data="disconnect_confirm_prompt")],
        [InlineKeyboardButton("⏰ Reminders", callback_data="settings_reminders")],
    ])
    await update.message.reply_text(
        f"*Your Notion settings*\n\nWorkspace: {workspace}\nDestination page: {page_display}",
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
            f"✅ All set! Your notes will be saved to 👉 *{page_title}*\n\n"
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
        page_display = f"*{page_title}*" if page_title else ("Selected ✅" if page_id else "_not selected yet_")
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Disconnect", callback_data="disconnect_confirm_prompt")],
            [InlineKeyboardButton("⏰ Reminders", callback_data="settings_reminders")],
        ])
        await query.edit_message_text(
            f"*Your Notion settings*\n\nWorkspace: {workspace}\nDestination page: {page_display}",
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
        page_id = _last_note_page.get(user.id)
        if not page_id:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                "This note is too old to attach a URL."
            )
            return
        _pending_url[user.id] = (page_id, time.monotonic() + _PENDING_URL_TTL_SEC)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "Send me the URL to add to your note, or /cancel to stop."
        )

    else:
        logger.warning("handle_callback: unrecognised callback_data=%s from user %s", data, user.id)


# ---------------------------------------------------------------------------
# Voice message handler
# ---------------------------------------------------------------------------

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming voice messages: auth check → download → transcribe → structure → Notion."""
    user = update.effective_user
    duration = update.message.voice.duration
    file_unique_id = update.message.voice.file_unique_id
    logger.info("Voice from user %s | duration=%ds", user.id, duration)

    if duration < 3:
        await update.message.reply_text("That voice clip was a little too short! ⚠️")
        return

    if user.id in _processing_users:
        await update.message.reply_text(
            "⏳ Your previous recording is still processing, please wait."
        )
        return

    # Clear any prior attachment state — new voice note supersedes it
    _last_note_page.pop(user.id, None)
    _pending_url.pop(user.id, None)

    file_path = f"/tmp/voice_{user.id}_{file_unique_id}.ogg"
    trimmed_path: str | None = None
    _processing_users.add(user.id)
    try:
        # Auth check before doing any work
        config_row = await get_user_config(user.id)
        if config_row is None:
            await update.message.reply_text(
                "You need to connect your Notion workspace first.\nUse /connect to get started."
            )
            return
        token, parent_page_id, _ = config_row
        if not parent_page_id:
            await update.message.reply_text(
                "You haven't selected a destination page yet.\nUse /settings to choose one."
            )
            return

        is_trimmed = duration > _MAX_VOICE_DURATION_SEC
        if is_trimmed:
            await update.message.reply_text(
                f"⚠️ Your note is over 3 min — I'll process the first 3 min only..."
            )

        voice_file = await update.message.voice.get_file()
        await voice_file.download_to_drive(file_path)
        file_size = os.path.getsize(file_path)
        logger.info("Downloaded to %s | size=%d bytes", file_path, file_size)

        # Telegram does not expose file size before download, so we check after
        if file_size > _MAX_VOICE_SIZE_BYTES:
            await update.message.reply_text(
                "Voice note file is too large. Please send a shorter recording."
            )
            return

        process_path = file_path
        if is_trimmed:
            trimmed_path = await _trim_audio(file_path, _MAX_VOICE_DURATION_SEC)
            process_path = trimmed_path

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
            except Exception:
                pass

        gem_task = asyncio.create_task(_gem_nudge(progress_msg))
        try:
            transcript = await transcribe_audio(process_path)
        finally:
            gem_task.cancel()

        # Whisper returns empty string for silence, pure noise, or sub-second clips
        if not transcript.strip():
            await update.message.reply_text(
                "I couldn't hear anything clearly — please try again."
            )
            return

        if config.ENABLE_TRANSCRIPT_CLEANING:
            before_len = len(transcript)
            transcript = clean_transcript(transcript)
            logger.info("Cleaning: %d→%d chars", before_len, len(transcript))

        if config.ENABLE_AI_STRUCTURING:
            await update.message.reply_text("Extracting the key points for you.")
            try:
                structured = await structure_transcript(transcript)
            except StructuringError as e:
                logger.warning("Structuring failed, falling back to plain transcript: %s", e)
                structured = {"title": "Voice note", "summary": transcript, "key_points": []}
        else:
            structured = {"title": "Voice note", "summary": transcript, "key_points": []}

        # Save to Notion
        await update.message.reply_text("Sending this straight to your Notion... 🧠")
        try:
            page_id, page_url = await create_page(token, parent_page_id, structured, transcript)
            _last_note_page[user.id] = page_id
            saved_line = "✅ Saved to Notion - only the first 3 min!" if is_trimmed else "✅ Saved to Notion!"
            await update.message.reply_text(
                f"{saved_line}\n\n"
                f"📄 {structured['title']}\n\n"
                f"{structured['summary']}\n\n"
                f"[{_truncate_url(page_url)}]({page_url})",
                parse_mode="Markdown",
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔗 Add URL", callback_data="add_url")],
                ]),
            )
        except NotionUnauthorizedError:
            logger.warning("Notion token expired for user %s", user.id)
            await update.message.reply_text(
                f"📄 {structured['title']}\n\n"
                f"{structured['summary']}\n\n"
                "⚠️ Notion connection expired. Use /connect to reconnect."
            )
        except NotionPageNotFoundError:
            logger.warning("Parent page deleted for user %s — clearing stored page", user.id)
            await save_parent_page(user.id, None)
            await update.message.reply_text(
                f"📄 {structured['title']}\n\n"
                f"{structured['summary']}\n\n"
                "⚠️ The destination page no longer exists in Notion. "
                "Use /settings to pick a new one."
            )
        except NotionError as e:
            logger.error("Notion save failed for user %s: %s", user.id, e)
            await update.message.reply_text(
                f"📄 {structured['title']}\n\n"
                f"{structured['summary']}\n\n"
                "⚠️ Transcribed but couldn't save to Notion. Please try again."
            )

    except Exception as e:
        logger.error("handle_voice failed for user %s: %s", user.id, e)
        await update.message.reply_text("Something went wrong. Please try again.")
    finally:
        _processing_users.discard(user.id)
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info("Cleaned up %s", file_path)
        if trimmed_path and os.path.exists(trimmed_path):
            os.remove(trimmed_path)
            logger.info("Cleaned up %s", trimmed_path)


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

    page_id, expires_at = _pending_url[user.id]
    if time.monotonic() > expires_at:
        _pending_url.pop(user.id, None)
        await update.message.reply_text(
            "⏳ Your attachment window expired. Send a voice note and try again."
        )
        return

    url_text = update.message.text.strip()
    _pending_url.pop(user.id)  # consume immediately — prevent double-submit

    config_row = await get_user_config(user.id)
    if config_row is None:
        await update.message.reply_text(
            "You need to connect your Notion workspace first.\nUse /connect to get started."
        )
        return
    token, _, _2 = config_row

    try:
        await append_url_block(token, page_id, url_text)
        await update.message.reply_text("✅ URL added to your note!")
    except NotionUnauthorizedError:
        await update.message.reply_text(
            "⚠️ Notion connection expired. Use /connect to reconnect."
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
        return web.Response(
            text=(
                "<html><body><h1>Something went wrong.</h1>"
                "<p>Please return to Telegram and try /connect again.</p></body></html>"
            ),
            content_type="text/html",
            status=500,
        )


async def _oauth_callback_inner(request: web.Request, ptb_app: Application) -> web.Response:
    """Core OAuth callback logic — called by _oauth_callback which wraps it in a catch-all."""
    code = request.rel_url.query.get("code")
    state = request.rel_url.query.get("state")

    if not code or not state:
        return web.Response(
            text="<html><body><h1>Invalid request — missing code or state.</h1></body></html>",
            content_type="text/html",
            status=400,
        )

    user_id = await pop_oauth_state(state)
    if user_id is None:
        logger.warning("OAuth callback received unknown state: %s", state)
        return web.Response(
            text=(
                "<html><body><h1>Unknown or expired session.</h1>"
                "<p>Please return to Telegram and use /connect again.</p></body></html>"
            ),
            content_type="text/html",
            status=400,
        )

    try:
        access_token, workspace_name = await exchange_token(code)
    except NotionOAuthError as e:
        logger.error("Token exchange failed for user %s: %s", user_id, e)
        return web.Response(
            text=(
                "<html><body><h1>Failed to connect to Notion.</h1>"
                "<p>Please return to Telegram and try /connect again.</p></body></html>"
            ),
            content_type="text/html",
            status=500,
        )

    await save_user_token(user_id, access_token, workspace_name)
    logger.info("OAuth complete for user %s (workspace: %s)", user_id, workspace_name)

    try:
        pages = await search_pages(access_token)
    except NotionError as e:
        logger.error("search_pages failed after OAuth for user %s: %s", user_id, e)
        pages = []

    if pages:
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
                        f"Connected to *{workspace_name}* 🎉\n\n"
                        f"Your notes will be saved to 👉 *{cached['title']}*\n\n"
                        "Send me a voice note anytime 🎤"
                    ),
                    parse_mode="Markdown",
                )
            else:
                # Cache miss — fall back to picker
                await ptb_app.bot.send_message(
                    chat_id=user_id,
                    text=f"Connected to *{workspace_name}* 🎉\n\nNow choose where to save your notes:",
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
        elif top_level:
            await ptb_app.bot.send_message(
                chat_id=user_id,
                text=f"Connected to *{workspace_name}* 🎉\n\nYou shared more than 1 page. Pick the one as your final destination.",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        else:
            await ptb_app.bot.send_message(
                chat_id=user_id,
                text=f"Connected to *{workspace_name}* 🎉\n\n{_NO_TOP_LEVEL_MSG}",
                parse_mode="Markdown",
            )
    else:
        await ptb_app.bot.send_message(
            chat_id=user_id,
            text=(
                f"Connected to *{workspace_name}* 🎉\n\n"
                "No pages found yet. Share a page with the QuiqDrop integration in Notion, "
                "then use /settings to select it."
            ),
            parse_mode="Markdown",
        )

    return web.Response(
        text=(
            "<html><body>"
            "<h1>Connected to Notion!</h1>"
            "<p>You can close this tab and return to Telegram.</p>"
            "</body></html>"
        ),
        content_type="text/html",
    )


def _create_web_app(ptb_app: Application) -> web.Application:
    """Create the aiohttp web application for OAuth callbacks and health checks."""
    app = web.Application()

    async def callback_handler(request: web.Request) -> web.Response:
        return await _oauth_callback(request, ptb_app)

    async def health_handler(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/oauth/notion/callback", callback_handler)
    app.router.add_get("/health", health_handler)
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
        try:
            await asyncio.Event().wait()  # run until Ctrl+C / SIGTERM
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task
            # Must stop before async with __aexit__ calls shutdown() — otherwise RuntimeError
            await ptb_app.updater.stop()
            await ptb_app.stop()

    await runner.cleanup()
    logger.info("Shutdown complete.")


def main() -> None:
    logger.info("Starting QuiqDrop bot...")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
