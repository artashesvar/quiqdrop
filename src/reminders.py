"""
Reminder message generation and sending.

- Daily reminder: fetches yesterday's Notion child pages, formats a list, sends at 9am.
- Weekly reminder: fetches last week's pages (Mon–Sun), formats a list, sends Monday at 9am.

Both functions raise on failure. The caller (reminder_scheduler._deliver_reminders) handles
failure counting and auto-disabling after _MAX_FAILURES consecutive errors.
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone

from src.db import ReminderUser
from src.notion import fetch_child_pages_in_range
from src.reminder_scheduler import calculate_local_time

logger = logging.getLogger(__name__)


def get_yesterday_date_range(timezone_str: str) -> dict:
    """
    Calculate yesterday's full-day date range in the user's local timezone,
    returned as UTC datetimes for use with the Notion API.

    Returns:
        {
            'date': 'Mar 23',          # display label
            'start': datetime (UTC),   # yesterday 00:00:00 local, as UTC
            'end':   datetime (UTC),   # yesterday 23:59:59 local, as UTC
        }
    """
    now_utc = datetime.now(timezone.utc)
    local_now = calculate_local_time(now_utc, timezone_str)

    # Midnight of today in local time, then go back one day
    local_today_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_yesterday_start = local_today_midnight - timedelta(days=1)
    local_yesterday_end = local_today_midnight - timedelta(seconds=1)

    # Convert back to UTC for the Notion query
    yesterday_start_utc = local_yesterday_start.astimezone(timezone.utc)
    yesterday_end_utc = local_yesterday_end.astimezone(timezone.utc)

    date_label = f"{local_yesterday_start.strftime('%b')} {local_yesterday_start.day}"  # e.g. "Mar 23"

    return {
        "date": date_label,
        "start": yesterday_start_utc,
        "end": yesterday_end_utc,
    }


def format_daily_reminder_message(date_str: str, notes: list[dict]) -> str:
    """
    Format the daily reminder message.

    Args:
        date_str: Display date, e.g. "Mar 23"
        notes: List of dicts with keys 'title' and 'url'

    Returns:
        Formatted message string matching the required spec.
    """
    header = f"🌅 Daily Reminder ({date_str})"

    if not notes:
        return (
            f"{header}\n\n"
            "No ideas captured yesterday. Start your day by sending a thought! 💭"
        )

    count = len(notes)
    noun = "idea" if count == 1 else "ideas"
    lines = [f"{header}\n\nYou captured {count} {noun} yesterday:\n"]
    for note in notes:
        lines.append(f"📄 {note['title']}")
        lines.append(note["url"])
    return "\n".join(lines)


def get_last_week_date_range(timezone_str: str) -> dict:
    """
    Calculate last week's date range (Monday–Sunday) in the user's local timezone,
    returned as UTC datetimes for use with the Notion API.

    Intended to be called on Monday morning. "Last week" means the 7-day window
    that ended at midnight last night (Sunday 23:59:59 → Monday 00:00:00 today).

    Example: called on Monday Mar 24 → range is Mar 17 00:00 – Mar 23 23:59:59.

    Returns:
        {
            'range_str': 'Mar 17 - Mar 23',
            'start': datetime (UTC),   # last Monday 00:00:00 local, as UTC
            'end':   datetime (UTC),   # last Sunday  23:59:59 local, as UTC
        }
    """
    now_utc = datetime.now(timezone.utc)
    local_now = calculate_local_time(now_utc, timezone_str)

    local_today_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_week_start = local_today_midnight - timedelta(days=7)   # last Monday 00:00
    local_week_end = local_today_midnight - timedelta(seconds=1)   # last Sunday 23:59:59

    week_start_utc = local_week_start.astimezone(timezone.utc)
    week_end_utc = local_week_end.astimezone(timezone.utc)

    range_str = (
        f"{local_week_start.strftime('%b')} {local_week_start.day}"
        f" - {local_week_end.strftime('%b')} {local_week_end.day}"
    )

    return {
        "range_str": range_str,
        "start": week_start_utc,
        "end": week_end_utc,
    }


def format_weekly_reminder_message(range_str: str, notes: list[dict]) -> str:
    """
    Format the weekly reminder message.

    Args:
        range_str: Display date range, e.g. "Mar 17 - Mar 23"
        notes: List of dicts with keys 'title' and 'url'

    Returns:
        Formatted message string matching the required spec.
    """
    header = f"📅 Weekly Summary ({range_str})"

    if not notes:
        return (
            f"{header}\n\n"
            "No ideas captured last week. Send a voice note to get started! 🎤"
        )

    count = len(notes)
    noun = "idea" if count == 1 else "ideas"
    lines = [f"{header}\n\nYou captured {count} {noun} last week! 🎉\n"]
    for note in notes:
        lines.append(f"📄 {note['title']}")
        lines.append(note["url"])
    lines.append("\nKeep capturing! 💡")
    return "\n".join(lines)


async def send_weekly_reminder(bot, user: ReminderUser) -> None:
    """
    Send the weekly reminder to a user (called on Monday mornings at 9am).

    Fetches last week's Notion child pages for the user's parent page,
    formats the message, and sends it via Telegram.

    Raises on any failure — caller (_deliver_reminders) handles failure counting.
    """
    if user.parent_page_id is None:
        logger.info(
            "Skipping weekly reminder for user %s — no parent page selected",
            user.telegram_user_id,
        )
        return

    date_range = get_last_week_date_range(user.time_zone)

    notes = await fetch_child_pages_in_range(
        token=user.notion_access_token,
        parent_id=user.parent_page_id,
        start_dt=date_range["start"],
        end_dt=date_range["end"],
    )

    message = format_weekly_reminder_message(date_range["range_str"], notes)

    await bot.send_message(
        chat_id=user.telegram_user_id,
        text=message,
        disable_web_page_preview=True,
    )
    logger.info(
        "Weekly reminder sent to user %s: %d note(s) for %s",
        user.telegram_user_id,
        len(notes),
        date_range["range_str"],
    )


async def send_daily_reminder(bot, user: ReminderUser) -> None:
    """
    Send the daily reminder to a user.

    Fetches yesterday's Notion child pages for the user's parent page,
    formats the message, and sends it via Telegram.

    Raises on any failure — caller (_deliver_reminders) handles failure counting.
    """
    if user.parent_page_id is None:
        logger.info(
            "Skipping daily reminder for user %s — no parent page selected",
            user.telegram_user_id,
        )
        return

    date_range = get_yesterday_date_range(user.time_zone)

    notes = await fetch_child_pages_in_range(
        token=user.notion_access_token,
        parent_id=user.parent_page_id,
        start_dt=date_range["start"],
        end_dt=date_range["end"],
    )

    message = format_daily_reminder_message(date_range["date"], notes)

    await bot.send_message(
        chat_id=user.telegram_user_id,
        text=message,
        disable_web_page_preview=True,
    )
    logger.info(
        "Daily reminder sent to user %s: %d note(s) for %s",
        user.telegram_user_id,
        len(notes),
        date_range["date"],
    )
