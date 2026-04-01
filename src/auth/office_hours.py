"""Office hours checker — determines if the tenant office is currently open."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

# Day names as returned by PF API
_DAYS_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def check_office_hours(office_hours: list[dict], timezone: str = "US/Eastern") -> dict:
    """Check if the current time falls within the tenant's office hours.

    Args:
        office_hours: List of dicts with ``day``, ``start_time``, ``end_time``,
            ``is_working`` from the PF login API.  Days not in the list are
            treated as closed.
        timezone: IANA timezone string (e.g. ``"US/Eastern"``).

    Returns:
        Dict with ``is_open``, ``current_day``, ``today_hours``, and ``next_open``.
    """
    if not office_hours:
        return {"is_open": True, "current_day": "", "today_hours": None, "next_open": ""}

    try:
        tz = ZoneInfo(timezone)
    except (KeyError, ValueError):
        tz = ZoneInfo("US/Eastern")

    now = datetime.now(tz)
    current_day = now.strftime("%A")  # e.g. "Monday"
    current_time = now.time()

    # Build lookup: day_name → {start_time, end_time, is_working}
    hours_by_day = {}
    for entry in office_hours:
        day = entry.get("day", "")
        if day:
            hours_by_day[day] = entry

    # Check today
    today = hours_by_day.get(current_day)
    today_hours = None
    is_open = False

    if today and today.get("is_working", False):
        start = _parse_time(today.get("start_time", ""))
        end = _parse_time(today.get("end_time", ""))
        if start and end and start <= current_time <= end:
            is_open = True
        if start and end:
            today_hours = {"start": start.strftime("%I:%M %p"), "end": end.strftime("%I:%M %p")}

    # Find next open window if currently closed
    next_open = ""
    if not is_open:
        next_open = _find_next_open(hours_by_day, current_day, current_time)

    return {
        "is_open": is_open,
        "current_day": current_day,
        "today_hours": today_hours,
        "next_open": next_open,
    }


def _parse_time(time_str: str) -> time | None:
    """Parse ``HH:MM:SS`` or ``HH:MM`` to a time object."""
    if not time_str:
        return None
    try:
        parts = time_str.split(":")
        return time(int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return None


def _find_next_open(hours_by_day: dict, current_day: str, current_time: time) -> str:
    """Find the next day/time the office opens."""
    try:
        today_idx = _DAYS_ORDER.index(current_day)
    except ValueError:
        return ""

    # Check if the office opens later today
    today = hours_by_day.get(current_day)
    if today and today.get("is_working", False):
        start = _parse_time(today.get("start_time", ""))
        if start and current_time < start:
            return f"today at {start.strftime('%I:%M %p').lstrip('0')}"

    # Check subsequent days (up to 7)
    for offset in range(1, 8):
        day_idx = (today_idx + offset) % 7
        day_name = _DAYS_ORDER[day_idx]
        entry = hours_by_day.get(day_name)
        if entry and entry.get("is_working", False):
            start = _parse_time(entry.get("start_time", ""))
            if start:
                label = "tomorrow" if offset == 1 else day_name
                return f"{label} at {start.strftime('%I:%M %p').lstrip('0')}"

    return ""
