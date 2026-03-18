"""Tests for date parsing utilities."""

import re
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from tools.date_utils import convert_natural_date, extract_date_range


class TestConvertNaturalDate:
    def test_empty_string(self):
        assert convert_natural_date("") is None

    def test_none_input(self):
        assert convert_natural_date(None) is None

    def test_iso_date(self):
        result = convert_natural_date("2026-03-15")
        assert result["start_date"] == "2026-03-15"
        assert result["end_date"] == "2026-03-15"
        assert result["strategy"] == "specific_day"

    def test_yyyy_mm(self):
        result = convert_natural_date("2026-04")
        assert result["start_date"] == "2026-04-01"
        assert result["end_date"] == "2026-04-30"
        assert result["strategy"] == "month"

    def test_today(self):
        today = datetime.now().strftime("%Y-%m-%d")
        result = convert_natural_date("today")
        assert result["start_date"] == today
        assert result["strategy"] == "specific_day"

    def test_tomorrow(self):
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        result = convert_natural_date("tomorrow")
        assert result["start_date"] == tomorrow
        assert result["strategy"] == "specific_day"

    def test_this_month(self):
        result = convert_natural_date("this month")
        assert result is not None
        assert result["strategy"] == "month"

    def test_next_month(self):
        result = convert_natural_date("next month")
        assert result is not None
        assert result["strategy"] == "month"
        # Should be a future month
        start = datetime.strptime(result["start_date"], "%Y-%m-%d")
        assert start > datetime.now()

    def test_next_week(self):
        result = convert_natural_date("next week")
        assert result is not None
        assert result["strategy"] == "week"
        start = datetime.strptime(result["start_date"], "%Y-%m-%d")
        assert start.weekday() == 0  # Monday

    def test_month_name_only(self):
        result = convert_natural_date("April")
        assert result is not None
        assert result["strategy"] == "month"
        assert "-04-" in result["start_date"]

    def test_month_name_with_day(self):
        result = convert_natural_date("March 15")
        assert result is not None
        assert result["strategy"] == "specific_day"
        assert result["start_date"].endswith("-03-15")

    def test_day_with_ordinal(self):
        result = convert_natural_date("Jan 10th")
        assert result is not None
        assert result["start_date"].endswith("-01-10")

    def test_last_week_of_month(self):
        result = convert_natural_date("last week of April")
        assert result is not None
        assert result["strategy"] == "week"
        assert "-04-" in result["start_date"]

    def test_ordinal_week(self):
        result = convert_natural_date("2nd week of April")
        assert result is not None
        assert result["strategy"] == "week"
        assert "-04-" in result["start_date"]

    def test_end_of_month(self):
        result = convert_natural_date("end of March")
        assert result is not None
        assert "-03-" in result["start_date"]

    def test_unrecognized_returns_none(self):
        assert convert_natural_date("asdfghjkl") is None


class TestExtractDateRange:
    def test_empty_string(self):
        assert extract_date_range("") is None

    def test_none_input(self):
        assert extract_date_range(None) is None

    def test_between_pattern(self):
        result = extract_date_range("between Jan 9 and Jan 18")
        assert result is not None
        assert result["start_date"].endswith("-01-09")
        assert result["end_date"].endswith("-01-18")

    def test_from_to_pattern(self):
        result = extract_date_range("from March 1 to March 15")
        assert result is not None
        assert result["start_date"].endswith("-03-01")
        assert result["end_date"].endswith("-03-15")

    def test_simple_to_pattern(self):
        result = extract_date_range("Jan 5 to Jan 12")
        assert result is not None

    def test_ordinal_dates(self):
        result = extract_date_range("between 1st Jan and 10th Jan")
        assert result is not None

    def test_no_range_returns_none(self):
        assert extract_date_range("next week") is None
