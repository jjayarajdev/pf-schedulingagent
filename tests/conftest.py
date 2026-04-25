"""Shared test fixtures for the scheduling bot."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure src/ is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Set test env vars before any imports
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("USE_DYNAMODB_STORAGE", "false")
os.environ.setdefault("PF_API_BASE_URL", "https://api-cx-portal.test.projectsforce.com")
os.environ.setdefault("SESSION_TABLE_NAME", "pf-syn-schedulingagents-sessions-test")
os.environ.setdefault("PHONE_CREDS_TABLE", "pf-syn-schedulingagents-phone-creds-test")
os.environ.setdefault("DYNAMODB_CONVERSATIONS_TABLE", "pf-syn-schedulingagents-conversations-test")
os.environ.setdefault("VAPI_ASSISTANTS_TABLE", "pf-syn-schedulingagents-vapi-assistants-test")
os.environ.setdefault("SMS_ORIGINATION_NUMBER", "+15551234567")
os.environ.setdefault("SMS_CONFIGURATION_SET", "scheduling-agent-sms-config-test")
os.environ.setdefault("OUTBOUND_CALLS_TABLE", "pf-syn-schedulingagents-outbound-calls-test")


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Clear Settings LRU cache between tests."""
    from config import get_settings, get_secrets

    get_settings.cache_clear()
    get_secrets.cache_clear()
    yield
    get_settings.cache_clear()
    get_secrets.cache_clear()


@pytest.fixture(autouse=True)
def _clear_auth_context():
    """Clear AuthContext between tests."""
    from auth.context import AuthContext

    yield
    AuthContext.clear()


@pytest.fixture(autouse=True)
def _clear_scheduling_caches():
    """Clear scheduling tool caches between tests."""
    from tools.scheduling import (
        _projects_cache,
        _request_id_by_project,
        _session_notes,
        _session_projects,
    )

    _projects_cache.clear()
    _request_id_by_project.clear()
    _session_notes.clear()
    _session_projects.clear()
    # Pre-populate for unit tests that call get_time_slots/confirm_appointment directly
    _request_id_by_project["123"] = 90001234
    yield
    _projects_cache.clear()
    _request_id_by_project.clear()
    _session_notes.clear()
    _session_projects.clear()


@pytest.fixture()
def mock_auth():
    """Set up AuthContext with test credentials."""
    from auth.context import AuthContext

    AuthContext.set(
        auth_token="test-jwt-token",
        client_id="test-client-123",
        customer_id="test-customer-456",
        user_id="test-user-789",
        user_name="Test User",
    )
    return AuthContext


@pytest.fixture()
def mock_httpx_response():
    """Factory for mock httpx responses."""

    def _make(status_code=200, json_data=None, text=""):
        resp = MagicMock()
        resp.status_code = status_code
        resp.text = text or (str(json_data) if json_data else "")
        resp.json.return_value = json_data or {}
        resp.raise_for_status = MagicMock()
        if status_code >= 400:
            import httpx

            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                "error", request=MagicMock(), response=resp
            )
        # For log_response compatibility
        resp.request = MagicMock()
        resp.request.method = "GET"
        resp.url = "https://api-cx-portal.test.projectsforce.com/test"
        return resp

    return _make


@pytest.fixture()
def mock_httpx_client(mock_httpx_response):
    """Context manager that patches httpx.AsyncClient for tool handler tests."""

    def _make(response=None, responses=None):
        """Create a patched httpx.AsyncClient.

        Args:
            response: Single mock response for all requests.
            responses: List of responses returned in order (for multi-request tools).
        """
        if response is None and responses is None:
            response = mock_httpx_response(200, {"status": "ok"})

        mock_client = AsyncMock()
        mock_context = AsyncMock()

        if responses:
            mock_client.get = AsyncMock(side_effect=responses)
            mock_client.post = AsyncMock(side_effect=responses)
            mock_client.put = AsyncMock(side_effect=responses)
        else:
            mock_client.get = AsyncMock(return_value=response)
            mock_client.post = AsyncMock(return_value=response)
            mock_client.put = AsyncMock(return_value=response)

        mock_context.__aenter__ = AsyncMock(return_value=mock_client)
        mock_context.__aexit__ = AsyncMock(return_value=False)

        patcher = patch("httpx.AsyncClient", return_value=mock_context)
        return patcher

    return _make
