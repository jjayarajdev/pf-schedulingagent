"""Tests for Vapi phone channel — webhook, tool calls, server events."""

import json
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _mock_vapi_secret():
    """Provide a test Vapi secret for authentication."""
    mock_secrets = MagicMock()
    mock_secrets.vapi_api_key = "test-vapi-secret-123"
    with patch("channels.vapi.get_secrets", return_value=mock_secrets):
        yield


def _vapi_headers():
    """Standard Vapi webhook headers with valid secret."""
    return {"x-vapi-secret": "test-vapi-secret-123"}


class TestVapiAuth:
    """Webhook authentication via x-vapi-secret header."""

    def test_missing_secret_rejected(self, client):
        resp = client.post("/vapi/webhook", json={"message": {"type": "status-update"}})
        assert resp.status_code == 401

    def test_wrong_secret_rejected(self, client):
        resp = client.post(
            "/vapi/webhook",
            json={"message": {"type": "status-update"}},
            headers={"x-vapi-secret": "wrong-secret"},
        )
        assert resp.status_code == 401

    def test_valid_secret_accepted(self, client):
        resp = client.post(
            "/vapi/webhook",
            json={"message": {"type": "status-update", "call": {"id": "c1"}}},
            headers=_vapi_headers(),
        )
        assert resp.status_code == 200

    def test_empty_secret_config_rejects(self, client):
        """If Vapi secret isn't configured, all requests are rejected."""
        mock_secrets = MagicMock()
        mock_secrets.vapi_api_key = ""
        with patch("channels.vapi.get_secrets", return_value=mock_secrets):
            resp = client.post(
                "/vapi/webhook",
                json={"message": {"type": "status-update"}},
                headers={"x-vapi-secret": "anything"},
            )
            assert resp.status_code == 401


class TestToolCalls:
    """Vapi tool-calls event (current format)."""

    @patch("channels.vapi._set_auth_context_from_phone", new_callable=AsyncMock)
    @patch("channels.vapi.get_orchestrator")
    def test_ask_scheduling_bot_success(self, mock_get_orch, mock_auth, client):
        """ask_scheduling_bot tool routes through orchestrator."""
        mock_response = MagicMock()
        mock_response.output = "You have **3 projects** ready to schedule."
        mock_orch = AsyncMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        mock_get_orch.return_value = mock_orch

        resp = client.post(
            "/vapi/webhook",
            json={
                "message": {
                    "type": "tool-calls",
                    "toolCalls": [{
                        "id": "tc-1",
                        "function": {
                            "name": "ask_scheduling_bot",
                            "arguments": {"question": "Show my projects"},
                        },
                    }],
                    "call": {
                        "id": "call-123",
                        "customer": {"number": "+14702832382"},
                    },
                },
            },
            headers=_vapi_headers(),
        )

        assert resp.status_code == 200
        body = resp.json()
        assert "results" in body
        assert len(body["results"]) == 1
        result_text = body["results"][0]["result"]
        assert "projects" in result_text.lower()
        assert body["results"][0]["toolCallId"] == "tc-1"
        # Voice format: no markdown
        assert "**" not in result_text

    @patch("channels.vapi._set_auth_context_from_phone", new_callable=AsyncMock)
    @patch("channels.vapi.get_orchestrator")
    def test_empty_question_returns_fallback(self, mock_get_orch, mock_auth, client):
        """Empty question returns a fallback message."""
        resp = client.post(
            "/vapi/webhook",
            json={
                "message": {
                    "type": "tool-calls",
                    "toolCalls": [{
                        "id": "tc-2",
                        "function": {
                            "name": "ask_scheduling_bot",
                            "arguments": {"question": ""},
                        },
                    }],
                    "call": {"id": "call-456"},
                },
            },
            headers=_vapi_headers(),
        )

        assert resp.status_code == 200
        result_text = resp.json()["results"][0]["result"]
        assert "trouble" in result_text.lower()

    @patch("channels.vapi._set_auth_context_from_phone", new_callable=AsyncMock)
    def test_unknown_tool_returns_error(self, mock_auth, client):
        """Unknown tool name returns error message."""
        resp = client.post(
            "/vapi/webhook",
            json={
                "message": {
                    "type": "tool-calls",
                    "toolCalls": [{
                        "id": "tc-3",
                        "function": {
                            "name": "nonexistent_tool",
                            "arguments": {},
                        },
                    }],
                    "call": {"id": "call-789"},
                },
            },
            headers=_vapi_headers(),
        )

        assert resp.status_code == 200
        result_text = resp.json()["results"][0]["result"]
        assert "unknown tool" in result_text.lower()

    @patch("channels.vapi._set_auth_context_from_phone", new_callable=AsyncMock)
    @patch("channels.vapi.get_orchestrator")
    def test_orchestrator_error_returns_fallback(self, mock_get_orch, mock_auth, client):
        """Orchestrator failure returns fallback message (not a 500)."""
        mock_orch = AsyncMock()
        mock_orch.route_request = AsyncMock(side_effect=RuntimeError("Bedrock timeout"))
        mock_get_orch.return_value = mock_orch

        resp = client.post(
            "/vapi/webhook",
            json={
                "message": {
                    "type": "tool-calls",
                    "toolCalls": [{
                        "id": "tc-4",
                        "function": {
                            "name": "ask_scheduling_bot",
                            "arguments": {"question": "Show projects"},
                        },
                    }],
                    "call": {"id": "call-err"},
                },
            },
            headers=_vapi_headers(),
        )

        assert resp.status_code == 200
        result_text = resp.json()["results"][0]["result"]
        assert "trouble" in result_text.lower()

    @patch("channels.vapi._set_auth_context_from_phone", new_callable=AsyncMock)
    def test_empty_tool_calls_list(self, mock_auth, client):
        """Empty toolCalls list returns empty results."""
        resp = client.post(
            "/vapi/webhook",
            json={
                "message": {
                    "type": "tool-calls",
                    "toolCalls": [],
                    "call": {"id": "call-empty"},
                },
            },
            headers=_vapi_headers(),
        )

        assert resp.status_code == 200
        assert resp.json()["results"] == []


class TestToolCallsAlternateFormats:
    """Vapi sends tool calls in different JSON shapes."""

    @patch("channels.vapi._set_auth_context_from_phone", new_callable=AsyncMock)
    @patch("channels.vapi.get_orchestrator")
    def test_tool_with_tool_call_list_format(self, mock_get_orch, mock_auth, client):
        """toolWithToolCallList format is parsed correctly."""
        mock_response = MagicMock()
        mock_response.output = "Here are your projects."
        mock_orch = AsyncMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        mock_get_orch.return_value = mock_orch

        resp = client.post(
            "/vapi/webhook",
            json={
                "message": {
                    "type": "tool-calls",
                    "toolWithToolCallList": [{
                        "name": "ask_scheduling_bot",
                        "toolCall": {
                            "id": "tc-alt-1",
                            "parameters": {"question": "Show projects"},
                        },
                    }],
                    "call": {"id": "call-alt"},
                },
            },
            headers=_vapi_headers(),
        )

        assert resp.status_code == 200
        mock_orch.route_request.assert_awaited_once()

    @patch("channels.vapi._set_auth_context_from_phone", new_callable=AsyncMock)
    @patch("channels.vapi.get_orchestrator")
    def test_arguments_as_string(self, mock_get_orch, mock_auth, client):
        """Arguments passed as JSON string instead of dict."""
        mock_response = MagicMock()
        mock_response.output = "Projects listed."
        mock_orch = AsyncMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        mock_get_orch.return_value = mock_orch

        resp = client.post(
            "/vapi/webhook",
            json={
                "message": {
                    "type": "tool-calls",
                    "toolCalls": [{
                        "id": "tc-str-1",
                        "function": {
                            "name": "ask_scheduling_bot",
                            "arguments": json.dumps({"question": "What are my projects?"}),
                        },
                    }],
                    "call": {"id": "call-str"},
                },
            },
            headers=_vapi_headers(),
        )

        assert resp.status_code == 200
        mock_orch.route_request.assert_awaited_once()


class TestFunctionCall:
    """Vapi function-call event (legacy format)."""

    @patch("channels.vapi._set_auth_context_from_phone", new_callable=AsyncMock)
    @patch("channels.vapi.get_orchestrator")
    def test_legacy_function_call(self, mock_get_orch, mock_auth, client):
        """Legacy functionCall format is handled correctly."""
        mock_response = MagicMock()
        mock_response.output = "Your appointment is tomorrow."
        mock_orch = AsyncMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        mock_get_orch.return_value = mock_orch

        resp = client.post(
            "/vapi/webhook",
            json={
                "message": {
                    "type": "function-call",
                    "functionCall": {
                        "name": "ask_scheduling_bot",
                        "parameters": {"question": "When is my appointment?"},
                    },
                    "toolCallId": "tc-legacy-1",
                    "call": {"id": "call-legacy"},
                },
            },
            headers=_vapi_headers(),
        )

        assert resp.status_code == 200
        body = resp.json()
        assert "results" in body
        result_text = body["results"][0]["result"]
        assert "appointment" in result_text.lower()


class TestServerEvents:
    """Vapi server URL events (end-of-call-report, status-update)."""

    def test_end_of_call_report(self, client):
        resp = client.post(
            "/vapi/webhook",
            json={
                "message": {
                    "type": "end-of-call-report",
                    "endedReason": "customer-ended-call",
                    "summary": "Customer asked about project scheduling.",
                    "cost": 0.05,
                    "call": {"id": "call-eocr-1"},
                },
            },
            headers=_vapi_headers(),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_status_update(self, client):
        resp = client.post(
            "/vapi/webhook",
            json={
                "message": {
                    "type": "status-update",
                    "status": "in-progress",
                    "call": {"id": "call-status-1"},
                },
            },
            headers=_vapi_headers(),
        )
        assert resp.status_code == 200

    def test_unknown_event_type(self, client):
        resp = client.post(
            "/vapi/webhook",
            json={
                "message": {
                    "type": "some-future-event",
                    "call": {"id": "call-unknown"},
                },
            },
            headers=_vapi_headers(),
        )
        assert resp.status_code == 200


class TestUnrecognizedPayload:
    """Unrecognized Vapi payloads."""

    def test_unrecognized_payload_returns_ok(self, client):
        """Unknown payload shape returns ok to prevent Vapi retries."""
        resp = client.post(
            "/vapi/webhook",
            json={"random_key": "value"},
            headers=_vapi_headers(),
        )
        assert resp.status_code == 200


class TestPhoneNumberExtraction:
    """_extract_phone_number from call metadata."""

    def test_customer_number(self):
        from channels.vapi import _extract_phone_number

        result = _extract_phone_number({
            "customer": {"number": "+14702832382"},
        })
        assert result == "+14702832382"

    def test_phone_number_dict(self):
        from channels.vapi import _extract_phone_number

        result = _extract_phone_number({
            "phoneNumber": {"number": "+15551234567"},
        })
        assert result == "+15551234567"

    def test_phone_number_string(self):
        from channels.vapi import _extract_phone_number

        result = _extract_phone_number({
            "phoneNumber": "+15559876543",
        })
        assert result == "+15559876543"

    def test_no_phone_number(self):
        from channels.vapi import _extract_phone_number

        result = _extract_phone_number({})
        assert result == ""

    def test_customer_takes_precedence(self):
        from channels.vapi import _extract_phone_number

        result = _extract_phone_number({
            "customer": {"number": "+11111111111"},
            "phoneNumber": {"number": "+22222222222"},
        })
        assert result == "+11111111111"


class TestBuildToolResult:
    """_build_tool_result response formatting."""

    def test_basic_result(self):
        from channels.vapi import _build_tool_result

        result = _build_tool_result("Hello world", "tc-1")
        assert result == {"results": [{"result": "Hello world", "toolCallId": "tc-1"}]}

    def test_newlines_collapsed(self):
        from channels.vapi import _build_tool_result

        result = _build_tool_result("Line 1\nLine 2\nLine 3", "tc-2")
        assert "\n" not in result["results"][0]["result"]

    def test_no_tool_call_id(self):
        from channels.vapi import _build_tool_result

        result = _build_tool_result("Hello", "")
        assert "toolCallId" not in result["results"][0]
