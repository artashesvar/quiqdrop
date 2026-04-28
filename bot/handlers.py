"""
handlers.py — All Telegram command and message handlers.

User flow:
    /start       → welcome + instructions
    /connect     → sends a Notion OAuth link so the user can connect their own account
    /setfolder   → lists the user's Notion pages (requires /connect first)
    /setlanguage → choose summary language
    voice msg    → transcribe → summarise → save to the user's Notion
"""

import os
import logging
import tempfile
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from bot import db, transcribe, summarize, notion_helper
from bot.oauth import get_auth_url

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hi! I'm your Voice-to-Notion bot.\n\n"
        "Here's how I work:\n"
        "1. Send me a voice message.\n"
        "2. I'll transcribe it, summarise it, and save it to *your* Notion.\n\n"
        "Before you start:\n"
        "  /connect — link your Notion account\n"
        "  /setfolder — choose which Notion page to save notes into\n"
        "  /setlanguage — choose the summary language\n\n"
        "⚠️ Your settings are saved persistently — you only need to set them once.",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# /connect
# ---------------------------------------------------------------------------

async def connect_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Send the user a Notion OAuth link so they can authorise the bot to access
    their own Notion workspace.

    After they click Allow in Notion, Notion redirects their browser to our
    callback URL. The web server in main.py picks that up and finishes the flow.
    """
    user_id = update.effective_user.id
    auth_url = get_auth_url(user_id)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Connect Notion", url=auth_url)]
    ])

    await update.message.reply_text(
        "Click the button below to connect your Notion account.\n\n"
        "You'll be taken to Notion to choose which workspace to use. "
        "Once you click *Allow*, come back here and run /setfolder.",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# /setfolder
# ---------------------------------------------------------------------------

async def setfolder_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Fetch the user's Notion pages (using their personal token) and display them
    as inline keyboard buttons so they can pick where notes get saved.
    """
    user_id = update.effective_user.id
    settings = db.get_settings(user_id)

    if not settings or not settings["notion_access_token"]:
        await update.message.reply_text(
            "⚠️ Please run /connect first to link your Notion account."
        )
        return

    await update.message.reply_text("🔍 Fetching your Notion pages…")

    try:
        pages = await notion_helper.list_top_pages(settings["notion_access_token"])
    except Exception:
        logger.exception("Failed to list Notion pages for user %s", user_id)
        await update.message.reply_text(
            "❌ Couldn't fetch your Notion pages. Try /connect again to re-link your account."
        )
        return

    if not pages:
        await update.message.reply_text(
            "😕 No Notion pages found.\n\n"
            "Make sure you selected at least one page when you authorised the bot in Notion. "
            "Run /connect again and this time tick the pages you want to use."
        )
        return

    # Store the id→name mapping so the callback can look up names without
    # cramming them into the 64-byte callback_data limit.
    context.user_data["pages"] = {p["id"]: p["name"] for p in pages}

    buttons = [
        [InlineKeyboardButton(p["name"], callback_data=f"folder:{p['id']}")]
        for p in pages
    ]
    await update.message.reply_text(
        "📂 Choose the Notion page where your voice notes will be saved:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ---------------------------------------------------------------------------
# /setlanguage
# ---------------------------------------------------------------------------

async def setlanguage_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🌐 Same as voice", callback_data="lang:same"),
            InlineKeyboardButton("🇬🇧 English", callback_data="lang:english"),
        ]
    ])
    await update.message.reply_text(
        "🗣 What language should the summary be written in?",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Callback handler (button taps)
# ---------------------------------------------------------------------------

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle inline keyboard button taps.

    Callback data formats:
        "folder:{page_id}"  → user selected a Notion folder
        "lang:{value}"      → user selected a summary language
    """
    query = update.callback_query
    await query.answer()

    data = query.data

    if data.startswith("folder:"):
        page_id = data[len("folder:"):]
        pages_map: dict = context.user_data.get("pages", {})
        page_name = pages_map.get(page_id, "Selected page")
        db.save_folder(query.from_user.id, page_id, page_name)
        await query.edit_message_text(
            f"✅ Got it! I'll save your notes to: *{page_name}*",
            parse_mode="Markdown",
        )

    elif data.startswith("lang:"):
        lang = data[len("lang:"):]
        db.save_language(query.from_user.id, lang)
        label = "same as voice 🌐" if lang == "same" else "English 🇬🇧"
        await query.edit_message_text(
            f"✅ Summary language set to: *{label}*",
            parse_mode="Markdown",
        )


# ---------------------------------------------------------------------------
# Voice message handler — the main pipeline
# ---------------------------------------------------------------------------

async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Full pipeline for incoming voice messages:
        1. Check the user has connected Notion (/connect) and picked a folder (/setfolder).
        2. Download the .ogg file from Telegram.
        3. Transcribe with Whisper.
        4. Summarise with Claude.
        5. Save a new page in the user's Notion.
        6. Confirm to the user.
        7. Clean up the temp file.
    """
    user_id = update.effective_user.id
    settings = db.get_settings(user_id)

    # Guard: Notion must be connected
    if not settings or not settings["notion_access_token"]:
        await update.message.reply_text(
            "⚠️ Please run /connect first to link your Notion account."
        )
        return

    # Guard: a destination folder must be chosen
    if not settings["notion_folder_id"]:
        await update.message.reply_text(
            "⚠️ Please run /setfolder first to choose where to save your notes."
        )
        return

    language = settings.get("language") or "same"

    await update.message.reply_text("⏳ Processing your voice note…")

    # --- Download ---
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ogg_path = os.path.join(tempfile.gettempdir(), f"voice_{user_id}_{timestamp}.ogg")

    try:
        voice_file = await update.message.voice.get_file()
        await voice_file.download_to_drive(ogg_path)
        logger.info("Downloaded voice file to %s", ogg_path)
    except Exception:
        logger.exception("Failed to download voice file for user %s", user_id)
        await update.message.reply_text("❌ Couldn't download your voice message. Please try again.")
        return

    # --- Transcribe ---
    try:
        transcript = await transcribe.transcribe_audio(ogg_path)
    except Exception:
        logger.exception("Whisper transcription failed for user %s", user_id)
        await update.message.reply_text("❌ Sorry, I couldn't transcribe your audio. Please try again.")
        _cleanup(ogg_path)
        return

    if not transcript:
        await update.message.reply_text("❌ The audio was empty or too short to transcribe.")
        _cleanup(ogg_path)
        return

    # --- Summarise ---
    try:
        summary = await summarize.summarize_text(transcript, language)
    except Exception:
        logger.exception("Claude summarisation failed for user %s", user_id)
        await update.message.reply_text("❌ Summarisation failed. Please try again.")
        _cleanup(ogg_path)
        return

    # --- Save to the user's Notion ---
    try:
        page_title = await notion_helper.create_note_page(
            token=settings["notion_access_token"],
            parent_id=settings["notion_folder_id"],
            transcript=transcript,
            summary=summary,
        )
    except Exception:
        logger.exception("Notion page creation failed for user %s", user_id)
        await update.message.reply_text(
            "❌ Couldn't save to Notion. Try running /connect again to refresh your connection."
        )
        _cleanup(ogg_path)
        return

    await update.message.reply_text(f"✅ Saved to Notion! *{page_title}*", parse_mode="Markdown")
    _cleanup(ogg_path)


def _cleanup(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
            logger.info("Cleaned up temp file: %s", path)
    except OSError:
        logger.warning("Could not delete temp file: %s", path)
