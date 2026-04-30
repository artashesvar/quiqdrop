# User config storage — async SQLite via aiosqlite
# DB lives at /data/quiqdrop.db — persisted via Railway volume.
from __future__ import annotations
import asyncio
import logging
import time
import typing
import aiosqlite

import src.config as config

logger = logging.getLogger(__name__)


class ReminderUser(typing.NamedTuple):
    telegram_user_id: int
    time_zone: str
    daily_reminder_enabled: bool
    weekly_reminder_enabled: bool
    notion_access_token: str
    parent_page_id: str | None

_DB_PATH = config.DB_PATH

# Single long-lived connection — avoids open/close + fsync per query.
# aiosqlite serializes calls on this connection internally, so concurrent
# coroutine access is safe for our (low-volume) workload.
_db: aiosqlite.Connection | None = None
_db_init_lock = asyncio.Lock()


async def _conn() -> aiosqlite.Connection:
    """Return the shared connection, opening it on first use."""
    global _db
    if _db is None:
        async with _db_init_lock:
            if _db is None:
                _db = await aiosqlite.connect(_DB_PATH)
    return _db


async def close_db() -> None:
    """Close the shared connection. Call on shutdown."""
    global _db
    if _db is not None:
        await _db.close()
        _db = None
        logger.info("Database connection closed")

_CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    telegram_user_id        INTEGER PRIMARY KEY,
    notion_access_token     TEXT    NOT NULL,
    workspace_name          TEXT    NOT NULL,
    parent_page_id          TEXT,
    parent_page_title       TEXT,
    -- Phase 6A: reminder preferences. time_zone format: "UTC", "UTC+4", "UTC-5".
    -- BOOLEAN stored as INTEGER (1/0) — SQLite has no native BOOLEAN type.
    -- reminder_failed_count increments on each delivery failure; resets on success.
    -- Reminders are auto-disabled after 7 consecutive failures (see reminder_scheduler._MAX_FAILURES).
    time_zone               TEXT    NOT NULL DEFAULT 'UTC',
    daily_reminder_enabled  INTEGER NOT NULL DEFAULT 0,
    weekly_reminder_enabled INTEGER NOT NULL DEFAULT 0,
    reminder_failed_count   INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
)
"""

# OAuth state tokens — persisted so bot restarts don't invalidate in-progress flows.
# expires_at is a Unix timestamp (INTEGER). Rows are deleted on pop or eviction.
_CREATE_PENDING_OAUTH_TABLE = """
CREATE TABLE IF NOT EXISTS pending_oauth (
    state           TEXT    PRIMARY KEY,
    telegram_user_id INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL
)
"""

# Applied one at a time; each is silently skipped if the column already exists.
_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN time_zone TEXT NOT NULL DEFAULT 'UTC'",
    "ALTER TABLE users ADD COLUMN daily_reminder_enabled INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN weekly_reminder_enabled INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN reminder_failed_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN parent_page_title TEXT",
]


async def init_db() -> None:
    """Create all tables if they don't exist, then apply column migrations. Call once at bot startup."""
    db = await _conn()
    await db.execute(_CREATE_USERS_TABLE)
    await db.execute(_CREATE_PENDING_OAUTH_TABLE)
    await db.commit()
    for migration in _MIGRATIONS:
        try:
            await db.execute(migration)
            await db.commit()
        except aiosqlite.OperationalError:
            # Column already exists — safe to ignore.
            pass
    logger.info("Database initialised at %s", _DB_PATH)


_OAUTH_TTL_SEC = 3600  # 1 hour — plenty of time to complete the OAuth flow


async def save_oauth_state(state: str, user_id: int) -> None:
    """Persist an OAuth state token mapped to a Telegram user ID."""
    expires_at = int(time.time()) + _OAUTH_TTL_SEC
    db = await _conn()
    await db.execute(
        "INSERT OR REPLACE INTO pending_oauth (state, telegram_user_id, expires_at) VALUES (?, ?, ?)",
        (state, user_id, expires_at),
    )
    await db.commit()


async def pop_oauth_state(state: str) -> tuple[int | None, bool]:
    """
    Remove and return the Telegram user ID for the given state token.

    Returns (user_id, was_expired):
      - (uid, False): valid state, deleted, ready to use
      - (uid, True):  state existed but was expired; deleted; do NOT exchange the code,
                       but the caller can still notify the user
      - (None, False): state never existed (or was already popped)
    """
    db = await _conn()
    async with db.execute(
        "SELECT telegram_user_id, expires_at FROM pending_oauth WHERE state = ?",
        (state,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None, False
    await db.execute("DELETE FROM pending_oauth WHERE state = ?", (state,))
    await db.commit()
    user_id, expires_at = row
    return user_id, int(time.time()) > expires_at


async def evict_stale_oauth_states() -> None:
    """Delete all expired OAuth state rows. Call periodically to keep the table clean."""
    db = await _conn()
    cursor = await db.execute(
        "DELETE FROM pending_oauth WHERE expires_at < ?",
        (int(time.time()),),
    )
    await db.commit()
    if cursor.rowcount:
        logger.debug("Evicted %d stale OAuth state(s)", cursor.rowcount)


async def save_user_token(user_id: int, token: str, workspace_name: str) -> None:
    """
    Upsert token and workspace name for a user.
    Resets parent_page_id and parent_page_title to NULL so the user picks a page
    fresh after each reconnect (and we never display a stale page name).
    """
    db = await _conn()
    await db.execute(
        """
        INSERT INTO users (telegram_user_id, notion_access_token, workspace_name, parent_page_id, parent_page_title)
        VALUES (?, ?, ?, NULL, NULL)
        ON CONFLICT(telegram_user_id) DO UPDATE SET
            notion_access_token = excluded.notion_access_token,
            workspace_name      = excluded.workspace_name,
            parent_page_id      = NULL,
            parent_page_title   = NULL,
            updated_at          = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        """,
        (user_id, token, workspace_name),
    )
    await db.commit()
    logger.info("Saved token for user %s (workspace: %s)", user_id, workspace_name)


async def save_parent_page(user_id: int, page_id: str, page_title: str = "") -> None:
    """Update the destination page for an existing user."""
    db = await _conn()
    cursor = await db.execute(
        """
        UPDATE users
        SET parent_page_id = ?, parent_page_title = ?,
            updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        WHERE telegram_user_id = ?
        """,
        (page_id, page_title, user_id),
    )
    await db.commit()
    if cursor.rowcount == 0:
        logger.warning("save_parent_page: no row found for user %s — token was never saved", user_id)
    else:
        logger.info("Saved parent page for user %s: %s", user_id, page_id)


async def get_user_config(user_id: int) -> tuple[str, str | None, str | None] | None:
    """
    Return (notion_access_token, parent_page_id, parent_page_title) for the user,
    or None if the user has not connected their workspace.
    parent_page_id / parent_page_title may be None if no page selected yet.
    """
    db = await _conn()
    async with db.execute(
        "SELECT notion_access_token, parent_page_id, parent_page_title FROM users WHERE telegram_user_id = ?",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return row[0], row[1], row[2]


async def get_workspace_name(user_id: int) -> str | None:
    """Return the connected workspace name for a user, or None if not found."""
    db = await _conn()
    async with db.execute(
        "SELECT workspace_name FROM users WHERE telegram_user_id = ?",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else None


async def delete_user_config(user_id: int) -> None:
    """Remove a user's config. Silently a no-op if the user is not found."""
    db = await _conn()
    await db.execute("DELETE FROM users WHERE telegram_user_id = ?", (user_id,))
    await db.commit()
    logger.info("Deleted config for user %s", user_id)


async def get_reminder_preferences(user_id: int) -> tuple[str, bool, bool] | None:
    """
    Return (time_zone, daily_reminder_enabled, weekly_reminder_enabled) for the user,
    or None if the user is not found.
    """
    db = await _conn()
    async with db.execute(
        "SELECT time_zone, daily_reminder_enabled, weekly_reminder_enabled FROM users WHERE telegram_user_id = ?",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return row[0], bool(row[1]), bool(row[2])


async def update_reminder_preferences(
    user_id: int,
    daily_enabled: bool | None = None,
    weekly_enabled: bool | None = None,
) -> None:
    """Update one or both reminder toggle flags for a user."""
    fields: list[str] = []
    values: list[object] = []
    if daily_enabled is not None:
        fields.append("daily_reminder_enabled = ?")
        values.append(1 if daily_enabled else 0)
    if weekly_enabled is not None:
        fields.append("weekly_reminder_enabled = ?")
        values.append(1 if weekly_enabled else 0)
    if not fields:
        return
    fields.append("updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')")
    values.append(user_id)
    sql = f"UPDATE users SET {', '.join(fields)} WHERE telegram_user_id = ?"
    db = await _conn()
    await db.execute(sql, values)
    await db.commit()
    logger.info("Updated reminder preferences for user %s (daily=%s, weekly=%s)", user_id, daily_enabled, weekly_enabled)


async def update_timezone(user_id: int, timezone: str) -> None:
    """Update the stored timezone for a user."""
    db = await _conn()
    await db.execute(
        "UPDATE users SET time_zone = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE telegram_user_id = ?",
        (timezone, user_id),
    )
    await db.commit()
    logger.info("Updated timezone for user %s: %s", user_id, timezone)


async def increment_failed_count(user_id: int) -> None:
    """Increment reminder_failed_count by 1 for a user."""
    db = await _conn()
    await db.execute(
        "UPDATE users SET reminder_failed_count = reminder_failed_count + 1, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE telegram_user_id = ?",
        (user_id,),
    )
    await db.commit()
    logger.info("Incremented failed reminder count for user %s", user_id)


async def reset_failed_count(user_id: int) -> None:
    """Reset reminder_failed_count to 0 for a user."""
    db = await _conn()
    await db.execute(
        "UPDATE users SET reminder_failed_count = 0, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE telegram_user_id = ?",
        (user_id,),
    )
    await db.commit()
    logger.info("Reset failed reminder count for user %s", user_id)


async def disable_reminders_for_user(user_id: int) -> None:
    """Disable both daily and weekly reminders (e.g. after 7 consecutive failed deliveries)."""
    db = await _conn()
    await db.execute(
        "UPDATE users SET daily_reminder_enabled = 0, weekly_reminder_enabled = 0, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE telegram_user_id = ?",
        (user_id,),
    )
    await db.commit()
    logger.info("Disabled all reminders for user %s", user_id)


async def get_failed_count(user_id: int) -> int:
    """Return the current reminder_failed_count for a user (0 if not found)."""
    db = await _conn()
    async with db.execute(
        "SELECT reminder_failed_count FROM users WHERE telegram_user_id = ?",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row else 0


async def get_users_with_reminders_enabled() -> list[ReminderUser]:
    """Return all users who have at least one reminder type enabled."""
    db = await _conn()
    async with db.execute(
        """
        SELECT telegram_user_id, time_zone, daily_reminder_enabled, weekly_reminder_enabled,
               notion_access_token, parent_page_id
        FROM users
        WHERE daily_reminder_enabled = 1 OR weekly_reminder_enabled = 1
        """
    ) as cursor:
        rows = await cursor.fetchall()
    return [
        ReminderUser(
            telegram_user_id=row[0],
            time_zone=row[1],
            daily_reminder_enabled=bool(row[2]),
            weekly_reminder_enabled=bool(row[3]),
            notion_access_token=row[4],
            parent_page_id=row[5],
        )
        for row in rows
    ]
