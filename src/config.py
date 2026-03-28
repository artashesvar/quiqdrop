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
    "NOTION_CLIENT_ID",
    "NOTION_CLIENT_SECRET",
    "NOTION_REDIRECT_URI",
]

# Fail at startup rather than at first use — better than a cryptic AttributeError mid-request
for _var in _REQUIRED:
    if not os.getenv(_var):
        raise RuntimeError(f"Missing required environment variable: {_var}")

TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_API_KEY: str = os.environ["OPENAI_API_KEY"]
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
NOTION_CLIENT_ID: str = os.environ["NOTION_CLIENT_ID"]
NOTION_CLIENT_SECRET: str = os.environ["NOTION_CLIENT_SECRET"]
NOTION_REDIRECT_URI: str = os.environ["NOTION_REDIRECT_URI"]
PORT: int = int(os.getenv("PORT", "8080"))
ENABLE_TRANSCRIPT_CLEANING: bool = os.getenv("ENABLE_TRANSCRIPT_CLEANING", "true").lower() == "true"  # defaults to true — set false in .env to inspect raw Whisper output
ENABLE_AI_STRUCTURING: bool = os.getenv("ENABLE_AI_STRUCTURING", "true").lower() == "true"  # defaults to true — set false in .env to skip structuring and reply with plain transcript

# Validate ANTHROPIC_API_KEY only when structuring is enabled — fail fast at startup
if ENABLE_AI_STRUCTURING and not ANTHROPIC_API_KEY:
    raise RuntimeError("Missing required environment variable: ANTHROPIC_API_KEY (required when ENABLE_AI_STRUCTURING=true)")
WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "whisper-1")  # whisper-1 is currently the only Whisper model OpenAI exposes via API
