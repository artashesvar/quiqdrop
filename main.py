"""
main.py — Entry point for the Voice-to-Notion Telegram bot.

Starts two async tasks concurrently via asyncio.gather():
    1. aiohttp web server — listens on PORT for the Notion OAuth redirect
    2. python-telegram-bot polling loop — handles all Telegram messages
"""

import asyncio
import logging
import os

from aiohttp import web
from dotenv import load_dotenv

# Load .env in local development. On Railway, env vars come from the dashboard.
load_dotenv()

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from bot import db
from bot.handlers import (
    start_handler,
    connect_handler,
    setfolder_handler,
    setlanguage_handler,
    callback_handler,
    voice_handler,
)
from bot.oauth import make_web_app

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def build_ptb_app() -> Application:
    """Create the Telegram Application and register all handlers."""
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("connect", connect_handler))
    app.add_handler(CommandHandler("setfolder", setfolder_handler))
    app.add_handler(CommandHandler("setlanguage", setlanguage_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.VOICE, voice_handler))
    return app


async def run_web_server(web_app: web.Application, port: int):
    """Start the aiohttp web server and keep it running."""
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logger.info("Web server listening on port %d", port)
    await asyncio.Event().wait()  # run forever until the process is stopped


async def run_bot(ptb_app: Application):
    """Initialise the Telegram bot and start polling for messages."""
    await ptb_app.initialize()
    await ptb_app.start()
    await ptb_app.updater.start_polling()
    logger.info("Telegram bot is polling for messages…")
    await asyncio.Event().wait()  # run forever until the process is stopped


async def main():
    db.init_db()
    logger.info("Database initialised")

    ptb_app = build_ptb_app()
    web_app = make_web_app(ptb_app)
    port = int(os.environ.get("PORT", 8080))

    # Run the web server and the Telegram bot at the same time.
    # asyncio.gather keeps both alive until the process is killed.
    await asyncio.gather(
        run_web_server(web_app, port),
        run_bot(ptb_app),
    )


if __name__ == "__main__":
    asyncio.run(main())
