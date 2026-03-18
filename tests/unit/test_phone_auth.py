"""Tests for phone authentication module."""

from auth.phone_auth import normalize_phone


class TestNormalizePhone:
    def test_us_with_plus_one(self):
        assert normalize_phone("+14702832382") == "4702832382"

    def test_us_with_one_prefix(self):
        assert normalize_phone("14702832382") == "4702832382"

    def test_already_10_digits(self):
        assert normalize_phone("4702832382") == "4702832382"

    def test_dashes_stripped(self):
        assert normalize_phone("1-470-283-2382") == "4702832382"

    def test_parentheses_stripped(self):
        assert normalize_phone("(470) 283-2382") == "4702832382"

    def test_india_code_stripped(self):
        assert normalize_phone("+918008455667") == "8008455667"

    def test_empty_string(self):
        assert normalize_phone("") == ""

    def test_none_like(self):
        assert normalize_phone("") == ""
