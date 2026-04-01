"""Tests for office hours checking utility."""

from datetime import datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from auth.office_hours import check_office_hours, _parse_time

# Sample office_hours from PF API
SAMPLE_HOURS = [
    {"hour_id": 449, "client_id": "09PF05VD", "day": "Monday", "start_time": "05:00:00", "end_time": "21:00:00", "is_working": True},
    {"hour_id": 450, "client_id": "09PF05VD", "day": "Tuesday", "start_time": "08:00:00", "end_time": "17:00:00", "is_working": True},
    {"hour_id": 451, "client_id": "09PF05VD", "day": "Wednesday", "start_time": "07:30:00", "end_time": "18:00:00", "is_working": True},
    {"hour_id": 452, "client_id": "09PF05VD", "day": "Thursday", "start_time": "08:30:00", "end_time": "23:45:00", "is_working": True},
    {"hour_id": 453, "client_id": "09PF05VD", "day": "Friday", "start_time": "12:00:00", "end_time": "17:00:00", "is_working": True},
    {"hour_id": 454, "client_id": "09PF05VD", "day": "Saturday", "start_time": "08:00:00", "end_time": "17:00:00", "is_working": True},
    # Sunday is absent — office closed
]

# 2026-03-30 is a Monday
_MONDAY = datetime(2026, 3, 30, tzinfo=ZoneInfo("US/Eastern"))


def _mock_now(day_name: str, hour: int, minute: int = 0):
    """Create a mock datetime for a specific day and time in US/Eastern."""
    day_offsets = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3, "Friday": 4, "Saturday": 5, "Sunday": 6}
    offset = day_offsets[day_name]
    dt = _MONDAY + timedelta(days=offset)
    return dt.replace(hour=hour, minute=minute)


class TestCheckOfficeHours:
    def test_within_hours_monday_afternoon(self):
        with patch("auth.office_hours.datetime") as mock_dt:
            mock_dt.now.return_value = _mock_now("Monday", 14, 0)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = check_office_hours(SAMPLE_HOURS, "US/Eastern")
        assert result["is_open"] is True
        assert result["current_day"] == "Monday"

    def test_outside_hours_tuesday_evening(self):
        with patch("auth.office_hours.datetime") as mock_dt:
            mock_dt.now.return_value = _mock_now("Tuesday", 18, 0)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = check_office_hours(SAMPLE_HOURS, "US/Eastern")
        assert result["is_open"] is False
        assert result["current_day"] == "Tuesday"

    def test_before_hours_friday_morning(self):
        with patch("auth.office_hours.datetime") as mock_dt:
            mock_dt.now.return_value = _mock_now("Friday", 8, 0)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = check_office_hours(SAMPLE_HOURS, "US/Eastern")
        assert result["is_open"] is False
        assert "today" in result["next_open"]

    def test_sunday_closed_no_entry(self):
        with patch("auth.office_hours.datetime") as mock_dt:
            mock_dt.now.return_value = _mock_now("Sunday", 12, 0)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = check_office_hours(SAMPLE_HOURS, "US/Eastern")
        assert result["is_open"] is False
        assert result["today_hours"] is None
        # Sunday → Monday is "tomorrow"
        assert "tomorrow" in result["next_open"]

    def test_at_boundary_start_time(self):
        with patch("auth.office_hours.datetime") as mock_dt:
            mock_dt.now.return_value = _mock_now("Wednesday", 7, 30)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = check_office_hours(SAMPLE_HOURS, "US/Eastern")
        assert result["is_open"] is True

    def test_at_boundary_end_time(self):
        with patch("auth.office_hours.datetime") as mock_dt:
            mock_dt.now.return_value = _mock_now("Wednesday", 18, 0)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = check_office_hours(SAMPLE_HOURS, "US/Eastern")
        assert result["is_open"] is True

    def test_empty_office_hours_defaults_open(self):
        result = check_office_hours([], "US/Eastern")
        assert result["is_open"] is True

    def test_next_open_saturday_evening(self):
        with patch("auth.office_hours.datetime") as mock_dt:
            mock_dt.now.return_value = _mock_now("Saturday", 20, 0)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = check_office_hours(SAMPLE_HOURS, "US/Eastern")
        assert result["is_open"] is False
        # Next open is Monday (Sunday is closed)
        assert "Monday" in result["next_open"]

    def test_is_working_false(self):
        hours_with_closed = [
            {"day": "Monday", "start_time": "08:00:00", "end_time": "17:00:00", "is_working": False},
        ]
        with patch("auth.office_hours.datetime") as mock_dt:
            mock_dt.now.return_value = _mock_now("Monday", 10, 0)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = check_office_hours(hours_with_closed, "US/Eastern")
        assert result["is_open"] is False

    def test_invalid_timezone_falls_back(self):
        with patch("auth.office_hours.datetime") as mock_dt:
            mock_dt.now.return_value = _mock_now("Monday", 14, 0)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            result = check_office_hours(SAMPLE_HOURS, "Invalid/Timezone")
        assert result["is_open"] is True


class TestParseTime:
    def test_hh_mm_ss(self):
        assert _parse_time("08:30:00") == time(8, 30)

    def test_hh_mm(self):
        assert _parse_time("14:00") == time(14, 0)

    def test_empty(self):
        assert _parse_time("") is None

    def test_invalid(self):
        assert _parse_time("not-a-time") is None
