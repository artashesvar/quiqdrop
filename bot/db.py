"""
db.py — SQLite persistence for user settings.

We use Python's built-in sqlite3 so there's no extra dependency.
The database file (users.db) is created automatically on first run.
Settings survive bot restarts and Railway redeployments.
"""

import sqlite3
import os

# The database file will be created in the project root directory.
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "users.db")


def init_db():
    """
    Create the database and the user_settings table if they don't exist yet.
    Call this once at bot startup.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id              INTEGER PRIMARY KEY,
                notion_access_token  TEXT,        -- OAuth token for this user's Notion account
                notion_folder_id     TEXT,        -- The Notion page chosen as the notes destination
                notion_folder_name   TEXT,        -- Human-readable name of that page
                language             TEXT DEFAULT 'same'  -- 'same' or 'english'
            )
        """)
        conn.commit()


def get_settings(user_id: int) -> dict | None:
    """
    Retrieve all settings for a user.
    Returns a dict, or None if the user hasn't set anything yet.
    """
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT notion_access_token, notion_folder_id, notion_folder_name, language "
            "FROM user_settings WHERE user_id = ?",
            (user_id,)
        ).fetchone()

    if row is None:
        return None

    return {
        "notion_access_token": row["notion_access_token"],
        "notion_folder_id":    row["notion_folder_id"],
        "notion_folder_name":  row["notion_folder_name"],
        "language":            row["language"] or "same",
    }


def save_token(user_id: int, access_token: str):
    """Save (or update) the user's Notion OAuth access token."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,)
        )
        conn.execute(
            "UPDATE user_settings SET notion_access_token = ? WHERE user_id = ?",
            (access_token, user_id)
        )
        conn.commit()


def save_folder(user_id: int, folder_id: str, folder_name: str):
    """Save the Notion page the user chose as their notes destination."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,)
        )
        conn.execute(
            "UPDATE user_settings SET notion_folder_id = ?, notion_folder_name = ? WHERE user_id = ?",
            (folder_id, folder_name, user_id)
        )
        conn.commit()


def save_language(user_id: int, language: str):
    """Save the user's preferred summary language ('same' or 'english')."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,)
        )
        conn.execute(
            "UPDATE user_settings SET language = ? WHERE user_id = ?",
            (language, user_id)
        )
        conn.commit()
