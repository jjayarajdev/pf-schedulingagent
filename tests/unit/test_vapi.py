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


class TestAssistantRequest:
    """Vapi assistant-request — customer vs. store detection."""

    @patch("channels.vapi.get_or_authenticate", new_callable=AsyncMock)
    def test_customer_gets_standard_config(self, mock_auth, client):
        """Known customer phone returns standard assistant config."""
        mock_auth.return_value = {
            "user_name": "Jane Smith",
            "client_name": "TestCo",
            "bearer_token": "tok",
        }
        resp = client.post(
            "/vapi/webhook",
            json={
                "message": {
                    "type": "assistant-request",
                    "call": {
                        "id": "call-cust-1",
                        "customer": {"number": "+14702832382"},
                    },
                },
            },
        )
        assert resp.status_code == 200
        assistant = resp.json()["assistant"]
        assert assistant["name"] == "ProjectsForce Scheduling Bot"
        assert "Jane" in assistant["firstMessage"]

    @patch("channels.vapi.get_or_authenticate", new_callable=AsyncMock)
    def test_store_caller_gets_store_config(self, mock_auth, client):
        """Unregistered phone (auth fails) returns store assistant config."""
        from auth.phone_auth import AuthenticationError

        mock_auth.side_effect = AuthenticationError("Not found", status_code=404)

        resp = client.post(
            "/vapi/webhook",
            json={
                "message": {
                    "type": "assistant-request",
                    "call": {
                        "id": "call-store-1",
                        "customer": {"number": "+15559999999"},
                        "phoneNumber": {"number": "+18001234567"},
                    },
                },
            },
        )
        assert resp.status_code == 200
        assistant = resp.json()["assistant"]
        assert assistant["name"] == "ProjectsForce Store Bot"
        assert "PO number" in assistant["firstMessage"]
        # Verify ask_store_bot tool is configured
        tool_names = [t["function"]["name"] for t in assistant["model"]["tools"]]
        assert "ask_store_bot" in tool_names

    @patch("channels.vapi.get_or_authenticate", new_callable=AsyncMock)
    def test_store_session_created(self, mock_auth, client):
        """Store detection creates a session entry in _store_sessions."""
        from channels.vapi import _store_sessions
        from auth.phone_auth import AuthenticationError

        mock_auth.side_effect = AuthenticationError("Not found", status_code=404)

        resp = client.post(
            "/vapi/webhook",
            json={
                "message": {
                    "type": "assistant-request",
                    "call": {
                        "id": "call-store-sess",
                        "customer": {"number": "+15559999999"},
                        "phoneNumber": {"number": "+18001234567"},
                    },
                },
            },
        )
        assert resp.status_code == 200
        assert "vapi-call-store-sess" in _store_sessions
        session = _store_sessions["vapi-call-store-sess"]
        assert session["authenticated"] is False
        # Cleanup
        _store_sessions.pop("vapi-call-store-sess", None)


class TestStoreToolCalls:
    """ask_store_bot tool call handling."""

    @patch("channels.vapi._set_auth_context_from_phone", new_callable=AsyncMock)
    def test_unauthenticated_no_lookup_prompts(self, mock_auth, client):
        """ask_store_bot without lookup info when unauthenticated prompts for it."""
        from channels.vapi import _store_sessions

        _store_sessions["vapi-call-store-tc1"] = {
            "to_phone": "+18001234567",
            "authenticated": False,
        }
        try:
            resp = client.post(
                "/vapi/webhook",
                json={
                    "message": {
                        "type": "tool-calls",
                        "toolCalls": [{
                            "id": "tc-store-1",
                            "function": {
                                "name": "ask_store_bot",
                                "arguments": {"question": "Show projects"},
                            },
                        }],
                        "call": {"id": "call-store-tc1"},
                    },
                },
                headers=_vapi_headers(),
            )
            assert resp.status_code == 200
            result = resp.json()["results"][0]["result"]
            assert "PO number" in result or "customer name" in result
        finally:
            _store_sessions.pop("vapi-call-store-tc1", None)

    @patch("channels.vapi._set_auth_context_from_phone", new_callable=AsyncMock)
    @patch("channels.vapi.authenticate_store", new_callable=AsyncMock)
    @patch("channels.vapi.get_orchestrator")
    def test_first_call_authenticates_and_queries(
        self, mock_get_orch, mock_store_auth, mock_phone_auth, client
    ):
        """First ask_store_bot with lookup info authenticates then queries."""
        from channels.vapi import _store_sessions

        mock_store_auth.return_value = {
            "bearer_token": "store-tok",
            "client_id": "09PF05VD",
            "customer_id": "1645869",
            "user_id": "1645869",
            "user_name": "Store User",
        }

        mock_response = MagicMock()
        mock_response.output = "Found 2 projects for this customer."
        mock_response.metadata = MagicMock()
        mock_response.metadata.agent_name = "SchedulingAgent"
        mock_orch = AsyncMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        mock_get_orch.return_value = mock_orch

        _store_sessions["vapi-call-store-tc2"] = {
            "to_phone": "+18001234567",
            "authenticated": False,
        }
        try:
            resp = client.post(
                "/vapi/webhook",
                json={
                    "message": {
                        "type": "tool-calls",
                        "toolCalls": [{
                            "id": "tc-store-2",
                            "function": {
                                "name": "ask_store_bot",
                                "arguments": {
                                    "question": "Show me the projects",
                                    "lookup_type": "po_number",
                                    "lookup_value": "PO-123",
                                },
                            },
                        }],
                        "call": {"id": "call-store-tc2"},
                    },
                },
                headers=_vapi_headers(),
            )
            assert resp.status_code == 200
            result = resp.json()["results"][0]["result"]
            assert "projects" in result.lower()
            # Session should now be authenticated
            assert _store_sessions["vapi-call-store-tc2"]["authenticated"] is True
        finally:
            _store_sessions.pop("vapi-call-store-tc2", None)

    @patch("channels.vapi._set_auth_context_from_phone", new_callable=AsyncMock)
    @patch("channels.vapi.get_orchestrator")
    def test_subsequent_calls_use_cached_creds(
        self, mock_get_orch, mock_phone_auth, client
    ):
        """After auth, subsequent ask_store_bot calls skip authenticate_store."""
        from channels.vapi import _store_sessions

        _store_sessions["vapi-call-store-tc3"] = {
            "to_phone": "+18001234567",
            "authenticated": True,
            "creds": {
                "bearer_token": "cached-store-tok",
                "client_id": "09PF05VD",
                "customer_id": "1645869",
                "user_id": "1645869",
                "user_name": "Store User",
            },
        }

        mock_response = MagicMock()
        mock_response.output = "Here are the available dates."
        mock_response.metadata = MagicMock()
        mock_response.metadata.agent_name = "SchedulingAgent"
        mock_orch = AsyncMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        mock_get_orch.return_value = mock_orch

        try:
            resp = client.post(
                "/vapi/webhook",
                json={
                    "message": {
                        "type": "tool-calls",
                        "toolCalls": [{
                            "id": "tc-store-3",
                            "function": {
                                "name": "ask_store_bot",
                                "arguments": {"question": "What dates are available?"},
                            },
                        }],
                        "call": {"id": "call-store-tc3"},
                    },
                },
                headers=_vapi_headers(),
            )
            assert resp.status_code == 200
            result = resp.json()["results"][0]["result"]
            assert "dates" in result.lower()
        finally:
            _store_sessions.pop("vapi-call-store-tc3", None)

    @patch("channels.vapi._set_auth_context_from_phone", new_callable=AsyncMock)
    @patch("channels.vapi.authenticate_store", new_callable=AsyncMock)
    def test_auth_failure_returns_friendly_message(
        self, mock_store_auth, mock_phone_auth, client
    ):
        """Failed store auth returns a helpful retry message."""
        from auth.phone_auth import AuthenticationError
        from channels.vapi import _store_sessions

        mock_store_auth.side_effect = AuthenticationError("Not found", status_code=404)

        _store_sessions["vapi-call-store-tc4"] = {
            "to_phone": "+18001234567",
            "authenticated": False,
        }
        try:
            resp = client.post(
                "/vapi/webhook",
                json={
                    "message": {
                        "type": "tool-calls",
                        "toolCalls": [{
                            "id": "tc-store-4",
                            "function": {
                                "name": "ask_store_bot",
                                "arguments": {
                                    "question": "Show projects",
                                    "lookup_type": "po_number",
                                    "lookup_value": "BAD-PO",
                                },
                            },
                        }],
                        "call": {"id": "call-store-tc4"},
                    },
                },
                headers=_vapi_headers(),
            )
            assert resp.status_code == 200
            result = resp.json()["results"][0]["result"]
            assert "couldn't find" in result.lower() or "double-check" in result.lower()
        finally:
            _store_sessions.pop("vapi-call-store-tc4", None)


class TestStoreSessionCleanup:
    """Store session cleanup on end-of-call."""

    def test_end_of_call_cleans_store_session(self, client):
        from channels.vapi import _store_sessions

        _store_sessions["vapi-call-cleanup-1"] = {
            "to_phone": "+18001234567",
            "authenticated": True,
            "creds": {"bearer_token": "tok"},
        }
        resp = client.post(
            "/vapi/webhook",
            json={
                "message": {
                    "type": "end-of-call-report",
                    "endedReason": "customer-ended-call",
                    "call": {"id": "call-cleanup-1"},
                },
            },
            headers=_vapi_headers(),
        )
        assert resp.status_code == 200
        assert "vapi-call-cleanup-1" not in _store_sessions

    def test_end_of_call_no_store_session_ok(self, client):
        """End-of-call for a non-store call doesn't error."""
        resp = client.post(
            "/vapi/webhook",
            json={
                "message": {
                    "type": "end-of-call-report",
                    "endedReason": "customer-ended-call",
                    "call": {"id": "call-no-store"},
                },
            },
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
