"""
oauth.py — Notion OAuth flow AND the aiohttp web server that receives the redirect.

How the OAuth flow works:
    1. User runs /connect in Telegram.
    2. Bot sends them a Notion authorization URL (get_auth_url).
    3. User clicks it, logs into Notion, picks their workspace, and clicks Allow.
    4. Notion redirects their browser to NOTION_REDIRECT_URI with ?code=...&state=...
    5. Our web server (make_web_app) receives that request at /notion/callback.
    6. We exchange the code for an access token and save it to SQLite.
    7. Bot sends the user a confirmation message in Telegram.
"""

import os
import base64
import logging
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

from bot import db

logger = logging.getLogger(__name__)

NOTION_CLIENT_ID = os.environ.get("NOTION_CLIENT_ID", "")
NOTION_CLIENT_SECRET = os.environ.get("NOTION_CLIENT_SECRET", "")
# Full public URL Notion will redirect to after the user authorises.
# Example: https://your-app.up.railway.app/notion/callback
NOTION_REDIRECT_URI = os.environ.get("NOTION_REDIRECT_URI", "")


# ---------------------------------------------------------------------------
# Step 1 — Build the authorization URL
# ---------------------------------------------------------------------------

def get_auth_url(user_id: int) -> str:
    """
    Return the Notion OAuth URL for this user.
    We embed the Telegram user_id as `state` so we can identify the user
    when Notion sends the redirect back to us.
    """
    params = {
        "owner": "user",
        "client_id": NOTION_CLIENT_ID,
        "redirect_uri": NOTION_REDIRECT_URI,
        "response_type": "code",
        "state": str(user_id),
    }
    return f"https://api.notion.com/v1/oauth/authorize?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Step 2 — Exchange the code for an access token
# ---------------------------------------------------------------------------

async def exchange_code(code: str) -> dict:
    """
    POST to Notion's token endpoint to swap the short-lived `code` for a
    long-lived access token.
    Notion requires HTTP Basic auth: base64(client_id:client_secret).
    """
    credentials = base64.b64encode(
        f"{NOTION_CLIENT_ID}:{NOTION_CLIENT_SECRET}".encode()
    ).decode()

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.notion.com/v1/oauth/token",
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/json",
            },
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": NOTION_REDIRECT_URI,
            },
        ) as resp:
            data = await resp.json()
            logger.info("Notion token exchange status: %s", resp.status)
            return data


# ---------------------------------------------------------------------------
# Step 3 — Handle the redirect, save token, notify user
# ---------------------------------------------------------------------------

async def handle_notion_callback(code: str, state: str, ptb_app):
    """
    Called when Notion redirects back to /notion/callback.
    Exchanges the code, saves the token, and pings the user in Telegram.
    """
    try:
        user_id = int(state)
    except (ValueError, TypeError):
        logger.error("Invalid state value in Notion callback: %r", state)
        return

    try:
        data = await exchange_code(code)
        token = data.get("access_token")
        workspace = data.get("workspace_name", "your workspace")

        if not token:
            logger.error("No access_token in Notion response: %s", data)
            await ptb_app.bot.send_message(
                user_id, "❌ Notion authorisation failed. Please try /connect again."
            )
            return

        db.save_token(user_id, token)
        logger.info("Saved Notion token for user %s (workspace: %s)", user_id, workspace)

        await ptb_app.bot.send_message(
            user_id,
            f"✅ Notion connected! (*{workspace}*)\n\n"
            "Now run /setfolder to choose which page your voice notes go into.",
            parse_mode="Markdown",
        )

    except Exception:
        logger.exception("Error in Notion OAuth callback for user %s", state)
        await ptb_app.bot.send_message(
            user_id,
            "❌ Something went wrong connecting to Notion. Please try /connect again."
        )


# ---------------------------------------------------------------------------
# aiohttp web server — lives here so all OAuth logic is in one place
# ---------------------------------------------------------------------------

def make_web_app(ptb_app) -> web.Application:
    """
    Create and return the aiohttp web application.

    Routes:
        GET /notion/callback  — Notion redirects here after the user authorises
        GET /health           — Railway health check
        GET /                 — also answers Railway's root health check
    """

    async def notion_callback(request: web.Request) -> web.Response:
        """Receive Notion's redirect and complete the OAuth flow."""
        code = request.rel_url.query.get("code")
        state = request.rel_url.query.get("state")   # Telegram user_id
        error = request.rel_url.query.get("error")

        if error or not code or not state:
            logger.warning("Notion OAuth redirect had error or missing params: %s", error)
            return web.Response(
                text="<h1>Authorization failed.</h1><p>You can close this tab and try /connect again in Telegram.</p>",
                content_type="text/html",
            )

        await handle_notion_callback(code, state, ptb_app)

        return web.Response(
            text="<h1>Connected! You can close this tab and return to Telegram.</h1>",
            content_type="text/html",
        )

    async def health(_request: web.Request) -> web.Response:
        """Simple health-check so Railway knows the service is alive."""
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/notion/callback", notion_callback)
    app.router.add_get("/health", health)
    app.router.add_get("/", health)
    return app
