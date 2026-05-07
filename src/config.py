# Environment variable loader
import os
from pathlib import Path
from urllib.parse import urlparse
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

# Base URL of our public web server (Coolify) — derived from the OAuth redirect URI.
# Used to host the /r/{path} deep-link bouncer that opens notion:// on mobile.
_parsed_redirect = urlparse(NOTION_REDIRECT_URI)
# Fail fast on misconfiguration — Notion rejects non-https redirect URIs anyway,
# but a clear startup error beats a confusing OAuth failure later.
if _parsed_redirect.scheme != "https":
    raise RuntimeError(
        f"NOTION_REDIRECT_URI must use https:// (got: {NOTION_REDIRECT_URI!r})"
    )
if not _parsed_redirect.netloc:
    raise RuntimeError(
        f"NOTION_REDIRECT_URI is missing a host (got: {NOTION_REDIRECT_URI!r})"
    )
_derived_base = f"{_parsed_redirect.scheme}://{_parsed_redirect.netloc}"
# PUBLIC_BASE_URL overrides the derived value — set this if NOTION_REDIRECT_URI points
# to a raw IP but you want a real domain in user-visible bouncer links.
BASE_URL: str = os.getenv("PUBLIC_BASE_URL", _derived_base).rstrip("/")
PORT: int = int(os.getenv("PORT", "8080"))
DB_PATH: str = os.getenv("DB_PATH", "/data/quiqdrop.db")
ENABLE_TRANSCRIPT_CLEANING: bool = os.getenv("ENABLE_TRANSCRIPT_CLEANING", "true").lower() == "true"  # defaults to true — set false in .env to inspect raw Whisper output
ENABLE_AI_STRUCTURING: bool = os.getenv("ENABLE_AI_STRUCTURING", "true").lower() == "true"  # defaults to true — set false in .env to skip structuring and reply with plain transcript

# Validate ANTHROPIC_API_KEY only when structuring is enabled — fail fast at startup
if ENABLE_AI_STRUCTURING and not ANTHROPIC_API_KEY:
    raise RuntimeError("Missing required environment variable: ANTHROPIC_API_KEY (required when ENABLE_AI_STRUCTURING=true)")
WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "whisper-1")  # whisper-1 is currently the only Whisper model OpenAI exposes via API

# Optional — base64-encoded 32-byte Fernet key used to encrypt Notion access tokens
# at rest in the SQLite DB. Generate with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# If unset, tokens are stored in plaintext (backwards-compat for existing deploys).
NOTION_TOKEN_KEY: str = os.getenv("NOTION_TOKEN_KEY", "")
