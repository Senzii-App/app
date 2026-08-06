"""Staff DB — staff profiles, certifications, and availability.
Direct port of src/db/staff.rs.
"""
from datetime import datetime, time
from typing import NamedTuple, Any

import asyncpg


class Certification(NamedTuple):
    name: str
    expires_at: datetime | None


class AvailabilityWindow(NamedTuple):
    day_of_week: int
    start_time: time
    end_time: time


def split_overnight_window(
    day_of_week: int,
    start_time: time,
    end_time: time,
) -> list[tuple[int, time, time]]:
    """Split an availability window that crosses midnight into two same-day segments."""
    if start_time < end_time:
        return [(day_of_week, start_time, end_time)]
    if start_time == end_time:
        return []
    midnight_end = time(23, 59, 59)
    midnight_start = time(0, 0, 0)
    next_day = (day_of_week + 1) % 7
    return [
        (day_of_week, start_time, midnight_end),
        (next_day, midnight_start, end_time),
    ]


def list_timezones() -> list[str]:
    """Return common IANA timezone names."""
    return [
        "UTC",
        "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
        "America/Anchorage", "Pacific/Honolulu",
        "America/Toronto", "America/Vancouver", "America/Mexico_City",
        "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Madrid",
        "Europe/Rome", "Europe/Amsterdam", "Europe/Stockholm",
        "Asia/Tokyo", "Asia/Seoul", "Asia/Shanghai", "Asia/Hong_Kong",
        "Asia/Singapore", "Asia/Kolkata", "Asia/Dubai", "Asia/Jerusalem",
        "Australia/Sydney", "Australia/Melbourne", "Australia/Perth",
        "Pacific/Auckland", "Pacific/Fiji",
    ]