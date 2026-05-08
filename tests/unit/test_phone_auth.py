"""Tests for phone authentication module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth.phone_auth import AuthenticationError, _call_auth_api, normalize_phone


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


def _mock_httpx_response(status_code: int, json_body: dict, text: str = ""):
    """Build an httpx.AsyncClient context manager that returns a single response."""
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json = MagicMock(return_value=json_body)
    mock_resp.text = text or str(json_body)

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    return mock_ctx


class TestAuthenticationErrorDefaults:
    def test_caller_type_defaults_to_store(self):
        exc = AuthenticationError("nope")
        assert exc.caller_type == "store"
        assert exc.store == {}

    def test_caller_type_passed_through(self):
        exc = AuthenticationError("nope", caller_type="unknown")
        assert exc.caller_type == "unknown"

    def test_store_payload_passed_through(self):
        store = {"store_id": 1, "store_name": "Test", "store_number": 99}
        exc = AuthenticationError("nope", caller_type="store", store=store)
        assert exc.store == store


class TestCallerTypeFromAPI:
    """Verifies _call_auth_api propagates caller_type for all three values."""

    @pytest.mark.asyncio
    async def test_user_caller_type_on_success(self):
        body = {
            "auth_status": "success",
            "caller_type": "user",
            "accesstoken": "tok",
            "user": {
                "customer_id": 60000086,
                "client_id": "09PF05VD",
                "first_name": "Test",
                "last_name": "Customer",
            },
            "client_id": "09PF05VD",
            "client_name": "ProjectsForce Validation",
            "timezone": "US/Eastern",
            "support_number": "(987) 987-9874",
        }
        with patch("httpx.AsyncClient", return_value=_mock_httpx_response(200, body)):
            creds = await _call_auth_api("5106269299", "8447845789")
        assert creds["caller_type"] == "user"
        assert creds["bearer_token"] == "tok"
        assert creds["customer_id"] == "60000086"

    @pytest.mark.asyncio
    async def test_store_caller_type_on_failed_auth(self):
        body = {
            "auth_status": "failed",
            "caller_type": "store",
            "store": {
                "store_id": 60008210,
                "store_name": "Test Store",
                "store_number": 1234,
            },
            "client_id": "09PF05VD",
            "client_name": "ProjectsForce Validation",
            "support_number": "(987) 987-9874",
            "timezone": "US/Eastern",
        }
        with (
            patch("httpx.AsyncClient", return_value=_mock_httpx_response(200, body)),
            pytest.raises(AuthenticationError) as exc_info,
        ):
                await _call_auth_api("3453232535", "8447845789")
        exc = exc_info.value
        assert exc.caller_type == "store"
        assert exc.store == body["store"]
        assert exc.client_id == "09PF05VD"
        assert exc.client_name == "ProjectsForce Validation"
        assert exc.support_number == "(987) 987-9874"

    @pytest.mark.asyncio
    async def test_unknown_caller_type_on_not_found(self):
        body = {
            "auth_status": "not_found",
            "caller_type": "unknown",
            "client_id": "09PF05VD",
            "client_name": "ProjectsForce Validation",
            "support_number": "(987) 987-9874",
        }
        with (
            patch("httpx.AsyncClient", return_value=_mock_httpx_response(200, body)),
            pytest.raises(AuthenticationError) as exc_info,
        ):
                await _call_auth_api("0000000000", "8447845789")
        exc = exc_info.value
        assert exc.caller_type == "unknown"
        assert exc.store == {}
        assert exc.client_id == "09PF05VD"
        assert exc.support_number == "(987) 987-9874"

    @pytest.mark.asyncio
    async def test_legacy_response_without_caller_type_falls_back(self):
        """Older PF API responses may not include caller_type — default to store."""
        body = {
            "auth_status": "failed",
            "client_id": "09PF05VD",
            "client_name": "ProjectsForce",
            "support_number": "(987) 987-9874",
        }
        with (
            patch("httpx.AsyncClient", return_value=_mock_httpx_response(200, body)),
            pytest.raises(AuthenticationError) as exc_info,
        ):
                await _call_auth_api("3453232535", "8447845789")
        assert exc_info.value.caller_type == "store"
