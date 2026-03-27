# Telegram listener and main entry point
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import src.config as config
from src.transcribe import transcribe_audio
from src.text_cleaner import clean_transcript

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Suppress httpx INFO logs — they contain the full bot token in the URL
logging.getLogger("httpx").setLevel(logging.WARNING)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start command."""
    user = update.effective_user
    logger.info("User %s sent /start", user.id)

    welcome = (
        f"Hey {user.first_name}! 👋\n\n"
        "I'm *QuiqDrop* — your voice-to-Notion assistant.\n\n"
        "Here's how it works:\n"
        "🎤 You send me a voice note\n"
        "✍️ I transcribe it with AI\n"
        "🧠 I structure it into a clean note\n"
        "📝 I save it directly to your Notion workspace\n\n"
        "Send me a voice note anytime to get started.\n"
        "_Full Notion setup coming soon!_"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming voice messages: download, transcribe, reply."""
    user = update.effective_user
    duration = update.message.voice.duration
    file_unique_id = update.message.voice.file_unique_id
    logger.info("Voice from user %s | duration=%ds", user.id, duration)

    file_path = f"/tmp/voice_{file_unique_id}.ogg"
    try:
        voice_file = await update.message.voice.get_file()
        await voice_file.download_to_drive(file_path)
        file_size = os.path.getsize(file_path)
        logger.info("Downloaded to %s | size=%d bytes", file_path, file_size)

        await update.message.reply_text("Transcribing your voice note...")
        transcript = await transcribe_audio(file_path)

        if config.ENABLE_TRANSCRIPT_CLEANING:
            raw_preview = transcript[:100]
            transcript = clean_transcript(transcript)
            logger.info("Before: %.100s | After: %.100s", raw_preview, transcript[:100])

        await update.message.reply_text(f"Transcript:\n\n{transcript}")

    except Exception as e:
        logger.error("handle_voice failed for user %s: %s", user.id, e)
        await update.message.reply_text("Something went wrong. Please try again.")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info("Cleaned up %s", file_path)


async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle any non-voice, non-command message."""
    if not update.message:
        return
    await update.message.reply_text(
        "Send me a voice note to get started 🎤"
    )


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log all unhandled handler errors so nothing fails silently in production."""
    logger.error("Update %s caused error: %s", update, context.error, exc_info=context.error)


def main() -> None:
    logger.info("Starting QuiqDrop bot...")

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.ALL, handle_unknown))
    app.add_error_handler(handle_error)

    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
