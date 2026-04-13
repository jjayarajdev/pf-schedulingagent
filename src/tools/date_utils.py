"""Natural language date parsing utilities — ported from v1.2.9."""

import calendar
import logging
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

MONTHS_MAP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def convert_natural_date(date_str: str) -> dict | None:
    """Convert natural language date to a date range dict.

    Returns:
        {"start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "strategy": str}
        or None if unparseable.
    """
    if not date_str:
        return None

    today = datetime.now()
    date_lower = date_str.lower().strip()

    # Already in YYYY-MM-DD format
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
        return {"start_date": date_str, "end_date": date_str, "strategy": "specific_day"}

    # YYYY-MM format
    yyyy_mm_match = re.match(r"^(\d{4})-(\d{2})$", date_str)
    if yyyy_mm_match:
        year, month = int(yyyy_mm_match.group(1)), int(yyyy_mm_match.group(2))
        if year == today.year and month == today.month:
            start = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            start = f"{year}-{month:02d}-01"
        days_in_month = calendar.monthrange(year, month)[1]
        end = f"{year}-{month:02d}-{days_in_month:02d}"
        return {"start_date": start, "end_date": end, "strategy": "month"}

    # "today"
    if date_lower in ("today",):
        d = today.strftime("%Y-%m-%d")
        return {"start_date": d, "end_date": d, "strategy": "specific_day"}

    # "tomorrow"
    if date_lower in ("tomorrow",):
        d = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        return {"start_date": d, "end_date": d, "strategy": "specific_day"}

    # "this month"
    if "this month" in date_lower:
        start = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        days_in_month = calendar.monthrange(today.year, today.month)[1]
        end = f"{today.year}-{today.month:02d}-{days_in_month:02d}"
        return {"start_date": start, "end_date": end, "strategy": "month"}

    # "next month"
    if "next month" in date_lower:
        if today.month == 12:
            year, month = today.year + 1, 1
        else:
            year, month = today.year, today.month + 1
        days_in_month = calendar.monthrange(year, month)[1]
        return {
            "start_date": f"{year}-{month:02d}-01",
            "end_date": f"{year}-{month:02d}-{days_in_month:02d}",
            "strategy": "month",
        }

    # "next week"
    if "next week" in date_lower:
        days_until_monday = (7 - today.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        next_monday = today + timedelta(days=days_until_monday)
        next_friday = next_monday + timedelta(days=4)
        return {
            "start_date": next_monday.strftime("%Y-%m-%d"),
            "end_date": next_friday.strftime("%Y-%m-%d"),
            "strategy": "week",
        }

    # "last week of [month]"
    last_week_match = re.search(r"last week of (\w+)", date_lower)
    if last_week_match:
        month_name = last_week_match.group(1)[:3].lower()
        if month_name in MONTHS_MAP:
            month_num = MONTHS_MAP[month_name]
            year = today.year if month_num >= today.month else today.year + 1
            days_in_month = calendar.monthrange(year, month_num)[1]
            start_day = days_in_month - 6
            return {
                "start_date": f"{year}-{month_num:02d}-{start_day:02d}",
                "end_date": f"{year}-{month_num:02d}-{days_in_month:02d}",
                "strategy": "week",
            }

    # Ordinal week: "1st week of January", "3rd week feb"
    ordinal_week_match = re.search(
        r"(1st|2nd|3rd|4th|5th|first|second|third|fourth|fifth)\s+week\s+(?:of|for)?\s*(\w+)", date_lower
    )
    if ordinal_week_match:
        week_ord = ordinal_week_match.group(1).lower()
        month_name = ordinal_week_match.group(2)[:3].lower()
        week_num_map = {
            "1st": 1, "first": 1, "2nd": 2, "second": 2, "3rd": 3, "third": 3,
            "4th": 4, "fourth": 4, "5th": 5, "fifth": 5,
        }
        if month_name in MONTHS_MAP and week_ord in week_num_map:
            month_num = MONTHS_MAP[month_name]
            week_num = week_num_map[week_ord]
            year = today.year if month_num >= today.month else today.year + 1

            first_day = datetime(year, month_num, 1)
            first_weekday = first_day.weekday()
            first_monday = 1 if first_weekday == 0 else 8 - first_weekday

            if week_num == 1:
                start_day = 1
            else:
                start_day = first_monday + (week_num - 2) * 7

            days_in_month = calendar.monthrange(year, month_num)[1]
            start_day = min(start_day, days_in_month)
            end_day = min(start_day + 6, days_in_month)

            start_date = f"{year}-{month_num:02d}-{start_day:02d}"
            end_date = f"{year}-{month_num:02d}-{end_day:02d}"

            # If start is in the past, adjust to tomorrow
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            tomorrow = today + timedelta(days=1)
            if start_dt.date() < tomorrow.date():
                end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                if end_dt.date() < tomorrow.date():
                    return {
                        "start_date": start_date,
                        "end_date": end_date,
                        "strategy": "week",
                        "past": True,
                    }
                start_date = tomorrow.strftime("%Y-%m-%d")

            return {"start_date": start_date, "end_date": end_date, "strategy": "week"}

    # "end of [month]"
    end_of_match = re.search(r"end of (\w+)", date_lower)
    if end_of_match:
        month_name = end_of_match.group(1)[:3].lower()
        if month_name in MONTHS_MAP:
            month_num = MONTHS_MAP[month_name]
            year = today.year if month_num >= today.month else today.year + 1
            days_in_month = calendar.monthrange(year, month_num)[1]
            start_day = days_in_month - 6
            return {
                "start_date": f"{year}-{month_num:02d}-{start_day:02d}",
                "end_date": f"{year}-{month_num:02d}-{days_in_month:02d}",
                "strategy": "week",
            }

    # Month names with optional day: "Jan 10", "10th Jan", "January", "March 15th"
    for name, num in MONTHS_MAP.items():
        if name in date_lower:
            day_match = re.search(r"(\d{1,2})(?:st|nd|rd|th)?", date_lower)
            if day_match:
                day = max(1, min(int(day_match.group(1)), 31))
                year = today.year if num >= today.month else today.year + 1
                d = f"{year}-{num:02d}-{day:02d}"
                return {"start_date": d, "end_date": d, "strategy": "specific_day"}
            else:
                year = today.year if num > today.month else (today.year if num == today.month else today.year + 1)
                if num == today.month and year == today.year:
                    start = (today + timedelta(days=1)).strftime("%Y-%m-%d")
                else:
                    start = f"{year}-{num:02d}-01"
                days_in_month = calendar.monthrange(year, num)[1]
                end = f"{year}-{num:02d}-{days_in_month:02d}"
                return {"start_date": start, "end_date": end, "strategy": "month"}

    logger.warning("Could not parse date preference: '%s'", date_str)
    return None


def extract_date_range(text: str) -> dict | None:
    """Extract start and end dates from range expressions like 'between Jan 9 and Jan 18'."""
    if not text:
        return None

    date_lower = text.lower().strip()
    today = datetime.now()

    date_part = r"(\d{1,2}(?:st|nd|rd|th)?\s+\w+|\w+\s+\d{1,2}(?:st|nd|rd|th)?)"
    range_patterns = [
        rf"between\s+{date_part}\s+(?:and|to)\s+{date_part}",
        rf"from\s+{date_part}\s+to\s+{date_part}",
        rf"{date_part}\s+to\s+{date_part}",
    ]

    def _parse_single_date(date_expr: str) -> str | None:
        date_expr = date_expr.strip().lower()
        day_match = re.search(r"(\d{1,2})(?:st|nd|rd|th)?", date_expr)
        if not day_match:
            return None
        day = max(1, min(int(day_match.group(1)), 31))
        month_num = None
        for month_name, num in MONTHS_MAP.items():
            if month_name in date_expr:
                month_num = num
                break
        if not month_num:
            return None
        year = today.year if month_num >= today.month else today.year + 1
        return f"{year}-{month_num:02d}-{day:02d}"

    for pattern in range_patterns:
        match = re.search(pattern, date_lower)
        if match:
            start_date = _parse_single_date(match.group(1))
            end_date = _parse_single_date(match.group(2))
            if start_date and end_date:
                return {"start_date": start_date, "end_date": end_date}

    return None
