"""
Background scheduler that sends daily and weekly reminders to users at 9am local time.
Runs as an asyncio task alongside the Telegram bot.
"""
from __future__ import annotations
import asyncio
import contextlib
import logging
import re
from datetime import datetime, timezone, timedelta

from src.db import (
    ReminderUser,
    disable_reminders_for_user,
    get_failed_count,
    get_users_with_reminders_enabled,
    increment_failed_count,
    reset_failed_count,
)

logger = logging.getLogger(__name__)

# After this many consecutive failed deliveries, reminders are permanently disabled for the user.
# 7 aligns with "one week of daily attempts" — enough to confirm the user has blocked the bot.
_MAX_FAILURES = 7

# Accepts "UTC", "UTC+0", "UTC+4", "UTC-5". No support for named zones (e.g. "Europe/Paris")
# — keeping timezone storage simple avoids pulling in pytz or zoneinfo for the MVP.
_TZ_RE = re.compile(r"^UTC([+-]\d+)?$")


def calculate_local_time(utc_time: datetime, timezone_str: str) -> datetime:
    """
    Convert UTC datetime to the user's local time.

    Accepted formats: "UTC", "UTC+0", "UTC+4", "UTC-5".
    Falls back to UTC on parse failure.
    """
    match = _TZ_RE.match(timezone_str.strip())
    if not match:
        logger.warning("Unrecognised timezone %r — falling back to UTC", timezone_str)
        return utc_time
    offset_str = match.group(1)
    offset_hours = int(offset_str) if offset_str else 0
    if not -12 <= offset_hours <= 14:
        logger.warning(
            "Timezone offset %d out of valid range for %r — falling back to UTC",
            offset_hours, timezone_str,
        )
        return utc_time
    tz = timezone(timedelta(hours=offset_hours))
    return utc_time.astimezone(tz)


async def _deliver_reminders(bot, user: ReminderUser, local_time: datetime) -> None:
    """
    Attempt to deliver whichever reminders are due for this user.

    Each reminder type is tried independently so a failure in one does not
    prevent the other from sending or corrupt the shared failure counter.

    On Mondays when weekly is enabled, daily is skipped — the weekly summary
    already covers Sunday's notes, so sending both would duplicate them.

    Tracks consecutive failures across all reminder types combined and
    disables reminders after _MAX_FAILURES consecutive failed check cycles.
    """
    # Local import avoids circular dependency (reminders.py imports calculate_local_time from here)
    from src.reminders import send_daily_reminder, send_weekly_reminder

    is_monday = local_time.weekday() == 0
    # Skip daily on Mondays when weekly is also enabled — weekly already covers Sunday.
    skip_daily_today = is_monday and user.weekly_reminder_enabled

    any_failure = False

    if user.daily_reminder_enabled and not skip_daily_today:
        try:
            await send_daily_reminder(bot, user)
        except Exception as exc:
            logger.error("Daily reminder failed for user %s: %s", user.telegram_user_id, exc)
            any_failure = True

    if user.weekly_reminder_enabled and is_monday:
        try:
            await send_weekly_reminder(bot, user)
        except Exception as exc:
            logger.error("Weekly reminder failed for user %s: %s", user.telegram_user_id, exc)
            any_failure = True

    if any_failure:
        await increment_failed_count(user.telegram_user_id)
        failed = await get_failed_count(user.telegram_user_id)
        if failed >= _MAX_FAILURES:
            await disable_reminders_for_user(user.telegram_user_id)
            logger.warning(
                "Disabled reminders for user %s after %d consecutive failures",
                user.telegram_user_id,
                _MAX_FAILURES,
            )
            with contextlib.suppress(Exception):
                await bot.send_message(
                    chat_id=user.telegram_user_id,
                    text=(
                        "⚠️ Your reminders have been automatically disabled after "
                        f"{_MAX_FAILURES} consecutive delivery failures.\n\n"
                        "Use /settings → ⏰ Reminders to re-enable them."
                    ),
                )
    else:
        await reset_failed_count(user.telegram_user_id)


async def check_and_send_reminders(bot) -> None:
    """
    Called every hour. Fetches all reminder-enabled users and sends
    reminders to those whose local time is 9am.
    """
    current_utc = datetime.now(timezone.utc)
    logger.info("Checking reminders at %s UTC", current_utc.strftime("%Y-%m-%dT%H:%M:%SZ"))

    users = await get_users_with_reminders_enabled()
    if not users:
        logger.debug("No users with reminders enabled")
        return

    for user in users:
        try:
            local_time = calculate_local_time(current_utc, user.time_zone)
            # Hour 9 = anywhere from 9:00 to 9:59 local time.
            # The hourly check means delivery is within 60 minutes of 9am — acceptable for a nudge.
            if local_time.hour == 9:
                await _deliver_reminders(bot, user, local_time)
        except Exception as exc:
            logger.error(
                "Unexpected error processing reminders for user %s: %s",
                user.telegram_user_id,
                exc,
            )


async def reminder_scheduler_loop(bot) -> None:
    """
    Main scheduler loop. Runs forever, checking for due reminders every hour.
    Designed to run as an asyncio background task alongside the bot.
    """
    logger.info("Reminder scheduler started")
    while True:
        try:
            await check_and_send_reminders(bot)
        except Exception as exc:
            logger.error("Unhandled error in reminder scheduler: %s", exc)
        # Sleep 1 hour between checks. Daily/weekly reminders don't need minute-level precision.
        await asyncio.sleep(3600)
