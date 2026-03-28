# Telegram listener and main entry point
import asyncio
import logging
import os
import secrets
import sys
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import src.config as config
from src.db import (
    delete_user_config,
    get_user_config,
    get_workspace_name,
    init_db,
    save_parent_page,
    save_user_token,
)
from src.notion import (
    NotionError,
    NotionOAuthError,
    NotionUnauthorizedError,
    create_page,
    exchange_token,
    search_pages,
)
from src.structure import StructuringError, structure_transcript
from src.text_cleaner import clean_transcript
from src.transcribe import transcribe_audio

_MAX_VOICE_DURATION_SEC = 300    # 5 minutes — beyond this Whisper quality degrades
_MAX_VOICE_SIZE_BYTES = 20 * 1024 * 1024  # 20MB — Whisper hard limit is 25MB

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Suppress httpx INFO logs — they contain the full bot token in the URL
logging.getLogger("httpx").setLevel(logging.WARNING)

# Maps random hex state token → telegram_user_id.
# In-memory: resets on restart (user clicks /connect again — acceptable for MVP).
# Entries for abandoned OAuth flows are never removed — negligible at MVP scale
# (~32 bytes per entry). Add TTL cleanup if the bot reaches thousands of daily users.
_pending_oauth: dict[str, int] = {}


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


def _build_page_keyboard(pages: list[dict]) -> InlineKeyboardMarkup:
    """
    Build an inline keyboard for page selection.
    Telegram enforces a hard 64-byte limit on callback_data.
    Format: select_page:{36-char UUID}:{title} — leaves ~14 chars for the title.
    """
    buttons = [
        [InlineKeyboardButton(
            p["title"][:40],  # display label can be longer
            callback_data=f"select_page:{p['id']}:{p['title'][:14]}",
        )]
        for p in pages
    ]
    return InlineKeyboardMarkup(buttons)


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


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start — greet user and show connection status."""
    user = update.effective_user
    logger.info("User %s sent /start", user.id)

    config_row = await get_user_config(user.id)
    if config_row is not None:
        _, page_id = config_row
        page_status = "selected ✅" if page_id else "not selected yet — use /settings"
        await update.message.reply_text(
            f"Welcome back, {user.first_name}! 👋\n\n"
            f"Your Notion workspace is connected.\n"
            f"Destination page: {page_status}\n\n"
            "Send me a voice note anytime 🎤"
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

    state = secrets.token_hex(16)
    _pending_oauth[state] = user.id

    url = _build_oauth_url(state)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Connect Notion Workspace", url=url)]
    ])
    await update.message.reply_text(
        "Click below to connect your Notion workspace:",
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

    _, page_id = config_row
    workspace = await get_workspace_name(user.id) or "Notion"
    page_display = "Selected ✅" if page_id else "_not selected yet_"
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Change Page", callback_data="change_page"),
            InlineKeyboardButton("Disconnect", callback_data="disconnect_confirm_prompt"),
        ]
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
# Callback query handler (inline keyboard buttons)
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all inline keyboard button presses."""
    query = update.callback_query
    # Must always be called — dismisses the Telegram loading spinner on the button
    await query.answer()

    user = query.from_user
    data = query.data

    if data.startswith("select_page:"):
        # format: select_page:{page_id}:{short_title}
        parts = data.split(":", 2)
        page_id = parts[1]
        page_title = parts[2] if len(parts) > 2 else "selected page"
        await save_parent_page(user.id, page_id)
        logger.info("User %s selected page %s", user.id, page_id)
        await query.edit_message_text(
            f"Got it. Saving all your notes to: *{page_title}* 📝\n\n"
            "Send me a voice note anytime 🎤",
            parse_mode="Markdown",
        )

    elif data == "change_page":
        config_row = await get_user_config(user.id)
        if config_row is None:
            await query.edit_message_text("You're not connected. Use /connect first.")
            return
        token, _ = config_row
        try:
            pages = await search_pages(token)
        except NotionError as e:
            logger.error("search_pages failed for user %s: %s", user.id, e)
            await query.edit_message_text("Couldn't fetch pages from Notion. Please try again.")
            return
        if not pages:
            await query.edit_message_text(
                "No pages found. Share at least one page with the QuiqDrop integration in Notion, "
                "then try again."
            )
            return
        await query.edit_message_text(
            "Choose where to save your notes:",
            reply_markup=_build_page_keyboard(pages),
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

    # Reject before downloading — saves bandwidth and API costs
    if duration > _MAX_VOICE_DURATION_SEC:
        await update.message.reply_text(
            f"Voice note is too long ({duration}s). Please keep it under 5 minutes."
        )
        return

    file_path = f"/tmp/voice_{user.id}_{file_unique_id}.ogg"
    try:
        # Auth check before doing any work
        config_row = await get_user_config(user.id)
        if config_row is None:
            await update.message.reply_text(
                "You need to connect your Notion workspace first.\nUse /connect to get started."
            )
            return
        token, parent_page_id = config_row
        if not parent_page_id:
            await update.message.reply_text(
                "You haven't selected a destination page yet.\nUse /settings to choose one."
            )
            return

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

        await update.message.reply_text("Transcribing your voice note...")
        transcript = await transcribe_audio(file_path)

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
            await update.message.reply_text("Structuring your note...")
            try:
                structured = await structure_transcript(transcript)
                reply = _format_structured(structured)
            except StructuringError as e:
                logger.warning("Structuring failed, falling back to plain transcript: %s", e)
                structured = {"title": "Voice note", "summary": transcript, "key_points": []}
                reply = f"Transcript:\n\n{transcript}"
        else:
            structured = {"title": "Voice note", "summary": transcript, "key_points": []}
            reply = f"Transcript:\n\n{transcript}"

        # Save to Notion
        try:
            _, page_url = await create_page(token, parent_page_id, structured, transcript)
            await update.message.reply_text(f"{reply}\n\n✅ Saved to Notion!\n{page_url}")
        except NotionUnauthorizedError:
            logger.warning("Notion token expired for user %s", user.id)
            await update.message.reply_text(
                f"{reply}\n\n⚠️ Notion connection expired. Use /connect to reconnect."
            )
        except NotionError as e:
            logger.error("Notion save failed for user %s: %s", user.id, e)
            await update.message.reply_text(
                f"{reply}\n\n⚠️ Transcribed but couldn't save to Notion. Please try again."
            )

    except Exception as e:
        logger.error("handle_voice failed for user %s: %s", user.id, e)
        await update.message.reply_text("Something went wrong. Please try again.")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info("Cleaned up %s", file_path)


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
    code = request.rel_url.query.get("code")
    state = request.rel_url.query.get("state")

    if not code or not state:
        return web.Response(
            text="<html><body><h1>Invalid request — missing code or state.</h1></body></html>",
            content_type="text/html",
            status=400,
        )

    user_id = _pending_oauth.pop(state, None)
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
        await ptb_app.bot.send_message(
            chat_id=user_id,
            text=f"Connected to *{workspace_name}* 🎉\n\nNow choose where to save your notes:",
            parse_mode="Markdown",
            reply_markup=_build_page_keyboard(pages),
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
    """Create the aiohttp web application for OAuth callbacks."""
    app = web.Application()

    async def callback_handler(request: web.Request) -> web.Response:
        return await _oauth_callback(request, ptb_app)

    app.router.add_get("/oauth/notion/callback", callback_handler)
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
    ptb_app.add_handler(CallbackQueryHandler(handle_callback))
    ptb_app.add_handler(MessageHandler(filters.VOICE, handle_voice))
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
        try:
            await asyncio.Event().wait()  # run until Ctrl+C / SIGTERM
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
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
