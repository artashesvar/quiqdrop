# User config storage — async SQLite via aiosqlite
# NOTE: DB lives at /tmp/quiqdrop.db — ephemeral on Railway (resets on redeploy).
# All users must re-authorise after a deploy. Acceptable for MVP.
# Migrate to persistent storage (e.g. Railway PostgreSQL) in a later phase.
from __future__ import annotations
import logging
import aiosqlite

logger = logging.getLogger(__name__)

_DB_PATH = "/tmp/quiqdrop.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    telegram_user_id    INTEGER PRIMARY KEY,
    notion_access_token TEXT    NOT NULL,
    workspace_name      TEXT    NOT NULL,
    parent_page_id      TEXT,
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
)
"""


async def init_db() -> None:
    """Create users table if it doesn't exist. Call once at bot startup."""
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(_CREATE_TABLE)
        await db.commit()
    logger.info("Database initialised at %s", _DB_PATH)


async def save_user_token(user_id: int, token: str, workspace_name: str) -> None:
    """
    Upsert token and workspace name for a user.
    Resets parent_page_id to NULL so the user selects a page after each reconnect.
    """
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (telegram_user_id, notion_access_token, workspace_name, parent_page_id)
            VALUES (?, ?, ?, NULL)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                notion_access_token = excluded.notion_access_token,
                workspace_name      = excluded.workspace_name,
                parent_page_id      = NULL,
                updated_at          = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            """,
            (user_id, token, workspace_name),
        )
        await db.commit()
    logger.info("Saved token for user %s (workspace: %s)", user_id, workspace_name)


async def save_parent_page(user_id: int, page_id: str) -> None:
    """Update the destination page for an existing user."""
    async with aiosqlite.connect(_DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE users
            SET parent_page_id = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE telegram_user_id = ?
            """,
            (page_id, user_id),
        )
        await db.commit()
        if cursor.rowcount == 0:
            logger.warning("save_parent_page: no row found for user %s — token was never saved", user_id)
        else:
            logger.info("Saved parent page for user %s: %s", user_id, page_id)


async def get_user_config(user_id: int) -> tuple[str, str | None] | None:
    """
    Return (notion_access_token, parent_page_id) for the user,
    or None if the user has not connected their workspace.
    parent_page_id may be None if the user hasn't selected a page yet.
    """
    async with aiosqlite.connect(_DB_PATH) as db:
        async with db.execute(
            "SELECT notion_access_token, parent_page_id FROM users WHERE telegram_user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return None
    return row[0], row[1]


async def get_workspace_name(user_id: int) -> str | None:
    """Return the connected workspace name for a user, or None if not found."""
    async with aiosqlite.connect(_DB_PATH) as db:
        async with db.execute(
            "SELECT workspace_name FROM users WHERE telegram_user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else None


async def delete_user_config(user_id: int) -> None:
    """Remove a user's config. Silently a no-op if the user is not found."""
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute("DELETE FROM users WHERE telegram_user_id = ?", (user_id,))
        await db.commit()
    logger.info("Deleted config for user %s", user_id)
