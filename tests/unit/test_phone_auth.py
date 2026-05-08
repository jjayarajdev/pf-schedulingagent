"""Tests for phone authentication module."""

from typing import ClassVar
from unittest.mock import patch

from auth.phone_auth import get_cached_auth, normalize_phone


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


class TestGetCachedAuthTenantAware:
    """Regression: get_cached_auth must build the same tenant-aware cache key
    that get_or_authenticate writes — otherwise end-of-call note posting
    silently fails ('No cached creds for call notes' warning).

    Bug: introduced in commit 319caca (multi-tenant cache isolation).
    Fix: pass to_phone so cache_key = f"{phone}:{to_phone}".
    """

    _CREDS: ClassVar[dict] = {
        "bearer_token": "tok",
        "client_id": "19PF06WT",
        "customer_id": "1751671",
        "user_id": "1751671",
    }

    def test_compound_key_with_to_phone(self):
        """When to_phone is provided, lookup uses 'phone:to_phone' compound key."""
        seen_keys = []

        def fake_get(key):
            seen_keys.append(key)
            return self._CREDS if key == "5104137024:8447844403" else None

        with patch("auth.phone_auth._get_cached_creds", side_effect=fake_get):
            result = get_cached_auth("5104137024", to_phone="+18447844403")

        assert result is not None
        assert result["bearer_token"] == "tok"
        assert seen_keys[0] == "5104137024:8447844403"

    def test_falls_back_to_phone_only_for_legacy_rows(self):
        """If compound-key miss but legacy phone-only row exists, use it."""
        seen_keys = []

        def fake_get(key):
            seen_keys.append(key)
            return self._CREDS if key == "5104137024" else None

        with patch("auth.phone_auth._get_cached_creds", side_effect=fake_get):
            result = get_cached_auth("5104137024", to_phone="+18447844403")

        assert result is not None
        assert seen_keys == ["5104137024:8447844403", "5104137024"]

    def test_phone_only_when_to_phone_missing(self):
        """Legacy callers passing only phone must keep working (no regression)."""
        seen_keys = []

        def fake_get(key):
            seen_keys.append(key)
            return self._CREDS if key == "5104137024" else None

        with patch("auth.phone_auth._get_cached_creds", side_effect=fake_get):
            result = get_cached_auth("5104137024")

        assert result is not None
        assert seen_keys == ["5104137024"]

    def test_returns_none_when_no_creds(self):
        with patch("auth.phone_auth._get_cached_creds", return_value=None):
            assert get_cached_auth("5104137024", to_phone="+18447844403") is None

    def test_to_phone_is_normalized(self):
        """to_phone with country code/punctuation should be normalized to bare digits."""
        seen_keys = []

        def fake_get(key):
            seen_keys.append(key)
            return None

        with patch("auth.phone_auth._get_cached_creds", side_effect=fake_get):
            get_cached_auth("5104137024", to_phone="+1 (844) 784-4403")

        # Should produce the SAME compound key as raw "8447844403"
        assert seen_keys[0] == "5104137024:8447844403"
