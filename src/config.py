# Environment variable loader
import os
from pathlib import Path
from dotenv import load_dotenv

# Always load .env from the project root, regardless of where the script is run from
_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env", override=True)

_REQUIRED = [
    "TELEGRAM_BOT_TOKEN",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "NOTION_CLIENT_ID",
    "NOTION_CLIENT_SECRET",
    "NOTION_REDIRECT_URI",
]

for _var in _REQUIRED:
    if not os.getenv(_var):
        raise RuntimeError(f"Missing required environment variable: {_var}")

TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY: str = os.environ["OPENAI_API_KEY"]
ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]
NOTION_CLIENT_ID: str = os.environ["NOTION_CLIENT_ID"]
NOTION_CLIENT_SECRET: str = os.environ["NOTION_CLIENT_SECRET"]
NOTION_REDIRECT_URI: str = os.environ["NOTION_REDIRECT_URI"]
PORT: int = int(os.getenv("PORT", "8080"))
