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
        assert assistant["name"] == "TestCo Scheduling Bot"
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
        assert assistant["name"] == "ProjectsForce Assistant"
        assert "How can I help you" in assistant["firstMessage"]
        # Verify ask_store_bot tool is configured
        tool_names = [
            t.get("function", {}).get("name", "") or t.get("type", "")
            for t in assistant["model"]["tools"]
        ]
        assert "ask_store_bot" in tool_names

    @patch("channels.vapi.get_or_authenticate", new_callable=AsyncMock)
    def test_store_caller_uses_client_name_from_error(self, mock_auth, client):
        """Store caller gets dynamic client_name from AuthenticationError."""
        from auth.phone_auth import AuthenticationError

        mock_auth.side_effect = AuthenticationError(
            "Not found", status_code=404, client_name="EQ Windows",
        )

        resp = client.post(
            "/vapi/webhook",
            json={
                "message": {
                    "type": "assistant-request",
                    "call": {
                        "id": "call-store-dyn",
                        "customer": {"number": "+15559999999"},
                    },
                },
            },
        )
        assert resp.status_code == 200
        assistant = resp.json()["assistant"]
        assert assistant["name"] == "EQ Windows Assistant"
        assert "EQ Windows" in assistant["firstMessage"]
        assert "EQ Windows" in assistant["endCallMessage"]

    @patch("channels.vapi.get_or_authenticate", new_callable=AsyncMock)
    def test_customer_dynamic_end_call_message(self, mock_auth, client):
        """Customer endCallMessage uses dynamic client_name."""
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
                        "id": "call-cust-dyn",
                        "customer": {"number": "+14702832382"},
                    },
                },
            },
        )
        assert resp.status_code == 200
        assistant = resp.json()["assistant"]
        assert "TestCo" in assistant["endCallMessage"]
        assert "TestCo" in assistant["voicemailMessage"]

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
            assert "project number" in result or "PO number" in result
        finally:
            _store_sessions.pop("vapi-call-store-tc1", None)

    @patch("channels.vapi._set_auth_context_from_phone", new_callable=AsyncMock)
    @patch("channels.vapi.authenticate_store", new_callable=AsyncMock)
    @patch("channels.vapi.get_orchestrator")
    def test_lookup_value_spaces_stripped(
        self, mock_get_orch, mock_store_auth, mock_phone_auth, client
    ):
        """STT-transcribed digits with spaces are stripped before API call."""
        from channels.vapi import _store_sessions

        mock_store_auth.return_value = {
            "bearer_token": "tok", "client_id": "C1",
            "customer_id": "1", "user_id": "1", "user_name": "Store",
        }
        mock_response = MagicMock()
        mock_response.output = "Found project."
        mock_response.metadata = MagicMock()
        mock_response.metadata.agent_name = "SchedulingAgent"
        mock_orch = AsyncMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        mock_get_orch.return_value = mock_orch

        _store_sessions["vapi-call-store-sp"] = {
            "to_phone": "+18001234567", "authenticated": False,
        }
        try:
            resp = client.post(
                "/vapi/webhook",
                json={
                    "message": {
                        "type": "tool-calls",
                        "toolCalls": [{
                            "id": "tc-sp",
                            "function": {
                                "name": "ask_store_bot",
                                "arguments": {
                                    "question": "Show projects",
                                    "lookup_type": "po_number",
                                    "lookup_value": "5 2 3 8 2 4",
                                },
                            },
                        }],
                        "call": {"id": "call-store-sp"},
                    },
                },
                headers=_vapi_headers(),
            )
            assert resp.status_code == 200
            # Verify spaces were stripped before calling authenticate_store
            call_args = mock_store_auth.call_args
            assert call_args[0][2] == "523824"  # lookup_value arg
        finally:
            _store_sessions.pop("vapi-call-store-sp", None)

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


class TestCallSummaryNotes:
    """End-of-call summary notes posted to PF project notes API."""

    @patch("channels.vapi.get_cached_auth")
    @patch("channels.vapi.post_call_summary_notes", new_callable=AsyncMock)
    @patch("channels.vapi.clear_session_projects")
    def test_end_of_call_posts_notes_when_creds_available(
        self, mock_clear, mock_post_notes, mock_get_auth, client,
    ):
        """End-of-call with summary + phone + creds triggers note posting."""
        mock_get_auth.return_value = {
            "bearer_token": "tok-123",
            "client_id": "CL1",
            "customer_id": "CUST1",
        }
        resp = client.post(
            "/vapi/webhook",
            json={
                "message": {
                    "type": "end-of-call-report",
                    "endedReason": "customer-ended-call",
                    "summary": "Scheduled fence installation for March 20.",
                    "cost": 0.25,
                    "durationSeconds": 120,
                    "call": {
                        "id": "call-notes-1",
                        "customer": {"number": "+15551234567"},
                    },
                },
            },
            headers=_vapi_headers(),
        )
        assert resp.status_code == 200
        # post_call_summary_notes should have been scheduled as a background task
        # (via asyncio.create_task, so the mock may or may not have been awaited
        # depending on event loop timing in sync TestClient)
        mock_get_auth.assert_called_once()

    @patch("channels.vapi.get_cached_auth", return_value=None)
    @patch("channels.vapi.clear_session_projects")
    def test_end_of_call_clears_session_when_no_creds(
        self, mock_clear, mock_get_auth, client,
    ):
        """When no cached creds, session projects are cleaned up."""
        resp = client.post(
            "/vapi/webhook",
            json={
                "message": {
                    "type": "end-of-call-report",
                    "endedReason": "customer-ended-call",
                    "summary": "Customer asked about scheduling.",
                    "durationSeconds": 30,
                    "call": {
                        "id": "call-notes-2",
                        "customer": {"number": "+15559876543"},
                    },
                },
            },
            headers=_vapi_headers(),
        )
        assert resp.status_code == 200
        mock_clear.assert_called_with("vapi-call-notes-2")

    @patch("channels.vapi.clear_session_projects")
    def test_end_of_call_no_summary_clears_session(self, mock_clear, client):
        """No summary → no note posting, just cleanup."""
        resp = client.post(
            "/vapi/webhook",
            json={
                "message": {
                    "type": "end-of-call-report",
                    "endedReason": "customer-ended-call",
                    "summary": "",
                    "call": {"id": "call-notes-3"},
                },
            },
            headers=_vapi_headers(),
        )
        assert resp.status_code == 200
        mock_clear.assert_called_with("vapi-call-notes-3")


class TestProjectTracking:
    """Per-session project action tracking in scheduling tools."""

    def test_track_and_retrieve(self):
        from tools.scheduling import (
            _session_projects,
            _track_project_action,
            clear_session_projects,
            get_session_projects,
        )

        # Simulate tracking with a patched session_id
        with patch("tools.scheduling.RequestContext") as mock_ctx:
            mock_ctx.get_session_id.return_value = "vapi-test-call"
            _track_project_action("90000149", "get_available_dates")
            _track_project_action("90000149", "get_time_slots")
            _track_project_action("90000116", "get_project_details")
            # Duplicate should not be added
            _track_project_action("90000149", "get_available_dates")

        result = get_session_projects("vapi-test-call")
        assert result == {
            "90000149": ["get_available_dates", "get_time_slots"],
            "90000116": ["get_project_details"],
        }

        clear_session_projects("vapi-test-call")
        assert get_session_projects("vapi-test-call") == {}

    def test_no_session_id_skips_tracking(self):
        from tools.scheduling import _track_project_action, get_session_projects

        with patch("tools.scheduling.RequestContext") as mock_ctx:
            mock_ctx.get_session_id.return_value = ""
            _track_project_action("90000149", "confirm_appointment")

        # Nothing should be tracked
        assert get_session_projects("") == {}


class TestPostCallSummaryNotes:
    """Integration test for post_call_summary_notes."""

    @pytest.mark.asyncio
    async def test_posts_note_per_project(self):
        from tools.scheduling import (
            clear_session_projects,
            get_session_projects,
            post_call_summary_notes,
        )

        # Pre-populate session projects
        with patch("tools.scheduling.RequestContext") as mock_ctx:
            mock_ctx.get_session_id.return_value = "vapi-test-notes"
            from tools.scheduling import _track_project_action
            _track_project_action("PROJ1", "get_available_dates")
            _track_project_action("PROJ1", "confirm_appointment")
            _track_project_action("PROJ2", "get_project_details")

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("tools.scheduling.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await post_call_summary_notes(
                session_id="vapi-test-notes",
                bearer_token="tok-abc",
                client_id="CL1",
                customer_id="CUST1",
                summary="Customer scheduled fence installation.",
                duration_seconds=165,
            )

            # Should have posted to 2 projects
            assert mock_client.post.call_count == 2

            # Verify note content for first call
            first_call_args = mock_client.post.call_args_list[0]
            url = first_call_args[0][0] if first_call_args[0] else first_call_args[1].get("url", "")
            payload = first_call_args[1].get("json", {})
            note = payload.get("note_text", "")
            assert "AI Scheduling Assistant (J)" in note
            assert "2m 45s" in note
            assert "Customer scheduled fence installation." in note
            assert "/communication/client/CL1/project/" in url

        # Session should be cleaned up
        assert get_session_projects("vapi-test-notes") == {}


class TestPostStoreCallNotes:
    """Store call notes use /project-notes/add-note endpoint."""

    @pytest.mark.asyncio
    async def test_posts_note_per_project(self):
        from tools.scheduling import (
            get_session_projects,
            post_store_call_notes,
        )

        # Track projects in a store session
        with patch("tools.scheduling.RequestContext") as mock_ctx:
            mock_ctx.get_session_id.return_value = "vapi-store-test"
            from tools.scheduling import _track_project_action
            _track_project_action("7751742", "get_available_dates")
            _track_project_action("7751743", "get_project_details")

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        with patch("tools.scheduling.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await post_store_call_notes(
                session_id="vapi-store-test",
                bearer_token="tok-store",
                client_id="09PF05VD",
                summary="Store confirmed delivery schedule.",
                duration_seconds=90,
            )

            assert mock_client.post.call_count == 2

            # Verify it uses /authentication/add-note endpoint
            first_call = mock_client.post.call_args_list[0]
            url = first_call[0][0] if first_call[0] else first_call[1].get("url", "")
            assert "/project-notes/add-note" in url

            # Verify payload has client_id, project_id (int), note_text
            payload = first_call[1].get("json", {})
            assert payload["client_id"] == "09PF05VD"
            assert isinstance(payload["project_id"], int)
            assert "Store called" in payload["note_text"]
            assert "1m 30s" in payload["note_text"]
            assert "Store confirmed delivery schedule." in payload["note_text"]

        assert get_session_projects("vapi-store-test") == {}

    @pytest.mark.asyncio
    async def test_skips_when_no_projects(self):
        from tools.scheduling import post_store_call_notes

        with patch("tools.scheduling.httpx.AsyncClient") as mock_client_cls:
            await post_store_call_notes(
                session_id="vapi-no-projects",
                bearer_token="tok",
                client_id="CL1",
                summary="Some summary",
            )
            mock_client_cls.assert_not_called()


class TestStoreEndOfCallNotes:
    """End-of-call handler routes store calls to /project-notes/add-note."""

    def test_store_call_uses_store_notes(self, client):
        """Store session end-of-call uses post_store_call_notes."""
        from channels.vapi import _store_sessions

        _store_sessions["vapi-store-eoc"] = {
            "to_phone": "+19566699322",
            "authenticated": True,
            "creds": {
                "bearer_token": "store-tok",
                "client_id": "09PF05VD",
                "customer_id": "123",
                "user_id": "456",
                "user_name": "Store Person",
            },
        }

        with patch("channels.vapi.post_store_call_notes") as mock_store_notes, \
             patch("channels.vapi.post_call_summary_notes") as mock_customer_notes:
            response = client.post(
                "/vapi/webhook",
                headers=_vapi_headers(),
                json={
                    "message": {
                        "type": "end-of-call-report",
                        "call": {"id": "store-eoc"},
                        "endedReason": "hangup",
                        "summary": "Store scheduled an appointment",
                        "cost": 0.05,
                        "durationSeconds": 60,
                    }
                },
            )

            assert response.status_code == 200
            mock_store_notes.assert_called_once()
            mock_customer_notes.assert_not_called()

            call_kwargs = mock_store_notes.call_args[1]
            assert call_kwargs["client_id"] == "09PF05VD"
            assert call_kwargs["bearer_token"] == "store-tok"

        _store_sessions.pop("vapi-store-eoc", None)

    def test_customer_call_uses_customer_notes(self, client):
        """Non-store end-of-call still uses post_call_summary_notes."""
        with patch("channels.vapi.post_call_summary_notes") as mock_customer_notes, \
             patch("channels.vapi.post_store_call_notes") as mock_store_notes, \
             patch("channels.vapi.get_cached_auth") as mock_auth:
            mock_auth.return_value = {
                "bearer_token": "cust-tok",
                "client_id": "CL1",
                "customer_id": "CUST1",
            }

            response = client.post(
                "/vapi/webhook",
                headers=_vapi_headers(),
                json={
                    "message": {
                        "type": "end-of-call-report",
                        "call": {
                            "id": "cust-eoc",
                            "customer": {"number": "+15551234567"},
                        },
                        "endedReason": "hangup",
                        "summary": "Customer asked about project",
                        "cost": 0.05,
                        "durationSeconds": 60,
                    }
                },
            )

            assert response.status_code == 200
            mock_store_notes.assert_not_called()
            mock_customer_notes.assert_called_once()


class TestNormalizeE164:
    """E.164 phone number normalization."""

    def test_10_digit_number(self):
        from channels.vapi import _normalize_e164

        assert _normalize_e164("5106269299") == "+15106269299"

    def test_formatted_number(self):
        from channels.vapi import _normalize_e164

        assert _normalize_e164("(510) 626-9299") == "+15106269299"

    def test_11_digit_with_leading_1(self):
        from channels.vapi import _normalize_e164

        assert _normalize_e164("15106269299") == "+15106269299"


class TestTransferCallTool:
    """transferCall tool injection for warm transfer (experimental mode)."""

    def test_transfer_tool_with_10_digit_number(self):
        from channels.vapi import _transfer_call_tool

        result = _transfer_call_tool("5106269299")
        assert len(result) == 1
        tool = result[0]
        assert tool["type"] == "transferCall"
        dest = tool["destinations"][0]
        assert dest["type"] == "number"
        assert dest["number"] == "+15106269299"
        assert dest["transferPlan"]["mode"] == "blind-transfer"

    def test_transfer_tool_with_formatted_number(self):
        from channels.vapi import _transfer_call_tool

        result = _transfer_call_tool("(510) 626-9299")
        dest = result[0]["destinations"][0]
        assert dest["number"] == "+15106269299"

    def test_transfer_tool_with_11_digit_number(self):
        from channels.vapi import _transfer_call_tool

        result = _transfer_call_tool("15106269299")
        dest = result[0]["destinations"][0]
        assert dest["number"] == "+15106269299"

    def test_transfer_tool_empty_number_returns_empty(self):
        from channels.vapi import _transfer_call_tool

        result = _transfer_call_tool("")
        assert result == []

    def test_blind_transfer_config(self):
        """Blind transfer has no transferAssistant — just mode and messages."""
        from channels.vapi import _transfer_call_tool

        result = _transfer_call_tool("5106269299")
        plan = result[0]["destinations"][0]["transferPlan"]
        assert plan["mode"] == "blind-transfer"
        assert "transferAssistant" not in plan

    def test_transfer_tool_caller_messages(self):
        """Caller should hear request-start, request-complete, and request-failed messages."""
        from channels.vapi import _transfer_call_tool

        result = _transfer_call_tool("5106269299")
        messages = result[0]["messages"]
        msg_types = {m["type"] for m in messages}
        assert "request-start" in msg_types
        assert "request-complete" in msg_types
        assert "request-failed" in msg_types

    def test_assistant_config_includes_transfer_tool(self):
        from channels.vapi import _build_assistant_config

        config = _build_assistant_config("Hello!", "secret", "3157613122")
        tools = config["model"]["tools"]
        transfer_tools = [t for t in tools if t.get("type") == "transferCall"]
        assert len(transfer_tools) == 1
        assert transfer_tools[0]["destinations"][0]["number"] == "+13157613122"

    def test_assistant_config_no_transfer_without_number(self):
        from channels.vapi import _build_assistant_config

        config = _build_assistant_config("Hello!", "secret", "")
        tools = config["model"]["tools"]
        transfer_tools = [t for t in tools if t.get("type") == "transferCall"]
        assert len(transfer_tools) == 0

    def test_send_support_sms_action_removed(self):
        from channels.vapi import _build_assistant_config

        config = _build_assistant_config("Hello!", "secret", "5551234567")
        ask_tool = config["model"]["tools"][0]
        props = ask_tool["function"]["parameters"]["properties"]
        assert "action" not in props

    def test_system_prompt_uses_transfer_call(self):
        from channels.vapi import _build_assistant_config

        config = _build_assistant_config("Hello!", "secret", "5551234567")
        prompt = config["model"]["messages"][0]["content"]
        assert "transferCall" in prompt
        assert "send_support_sms" not in prompt


# ── Outbound call tests ──────────────────────────────────────────────


class TestOutboundGreeting:
    def test_greeting_with_name_and_project(self):
        from channels.vapi import _generate_outbound_greeting

        greeting = _generate_outbound_greeting("Jane Doe", "FloorCo", "Flooring Installation")
        assert "Jane" in greeting
        assert "FloorCo" in greeting
        assert "Flooring Installation" in greeting
        assert "good time" in greeting.lower()

    def test_greeting_without_name(self):
        from channels.vapi import _generate_outbound_greeting

        greeting = _generate_outbound_greeting("", "FloorCo", "Carpet")
        assert "Hello!" in greeting
        assert "FloorCo" in greeting

    def test_greeting_without_project_type(self):
        from channels.vapi import _generate_outbound_greeting

        greeting = _generate_outbound_greeting("Jane", "FloorCo", "")
        assert "Jane" in greeting
        assert "upcoming project" in greeting.lower()


class TestOutboundSchedulingConfig:
    def test_config_structure(self):
        from channels.vapi import _build_outbound_scheduling_config

        outbound = {
            "customer_name": "Jane Doe",
            "project_type": "Flooring",
        }
        config = _build_outbound_scheduling_config(
            "Hello Jane!", "secret", outbound, "+15551234567", "FloorCo",
        )

        assert config["name"] == "FloorCo Outbound Scheduling"
        assert config["firstMessage"] == "Hello Jane!"
        assert config["voice"] is not None
        tool_names = [t["function"]["name"] for t in config["model"]["tools"] if t.get("type") == "function"]
        assert "get_time_slots" in tool_names
        assert "confirm_appointment" in tool_names
        assert "add_note" in tool_names
        assert "server" in config
        assert config["server"]["secret"] == "secret"

    def test_config_has_voicemail_message(self):
        from channels.vapi import _build_outbound_scheduling_config

        outbound = {"customer_name": "Jane Doe", "project_type": "Windows"}
        config = _build_outbound_scheduling_config(
            "Hello!", "secret", outbound, "+15551234567",
        )

        assert "voicemailMessage" in config
        assert "Jane" in config["voicemailMessage"]
        assert "Windows" in config["voicemailMessage"]

    def test_config_has_transfer_tool_with_support_number(self):
        from channels.vapi import _build_outbound_scheduling_config

        outbound = {"customer_name": "Jane", "project_type": "Carpet"}
        config = _build_outbound_scheduling_config(
            "Hello!", "secret", outbound, "+15551234567",
        )

        tools = config["model"]["tools"]
        transfer_tools = [t for t in tools if t.get("type") == "transferCall"]
        assert len(transfer_tools) == 1

    def test_config_no_transfer_without_number(self):
        from channels.vapi import _build_outbound_scheduling_config

        outbound = {"customer_name": "Jane", "project_type": "Carpet"}
        config = _build_outbound_scheduling_config(
            "Hello!", "secret", outbound, "",
        )

        tools = config["model"]["tools"]
        transfer_tools = [t for t in tools if t.get("type") == "transferCall"]
        assert len(transfer_tools) == 0

    def test_system_prompt_has_call_flow_steps(self):
        from channels.vapi import _build_outbound_scheduling_config

        outbound = {"customer_name": "Jane Doe", "project_type": "Flooring"}
        config = _build_outbound_scheduling_config(
            "Hello!", "secret", outbound, "+15551234567",
        )

        prompt = config["model"]["messages"][0]["content"]
        assert "Step 1" in prompt
        assert "Step 2" in prompt
        assert "Step 3" in prompt
        assert "Step 4" in prompt
        assert "Step 5" in prompt
        assert "Flooring" in prompt
        assert "STYLE RULES" in prompt
        assert "TOOL RULES" in prompt


class TestOutboundPrefetchPrompt:
    """Test that pre-fetched data is injected into the outbound prompt."""

    def test_prefetched_dates_in_prompt(self):
        from channels.vapi import _build_outbound_scheduling_config

        outbound = {
            "customer_name": "Jane Doe",
            "project_type": "Flooring",
            "prefetched": {
                "dates": {
                    "available_dates": ["2026-04-14", "2026-04-15"],
                    "available_time_slots": ["9:00 AM", "1:00 PM"],
                    "request_id": 42,
                },
                "address": {
                    "address1": "123 Main St",
                    "city": "Austin",
                    "state": "TX",
                    "zipcode": "78701",
                },
            },
        }
        config = _build_outbound_scheduling_config(
            "Hello!", "secret", outbound, "+15551234567",
        )
        prompt = config["model"]["messages"][0]["content"]

        # Dates should be in the prompt as a range summary
        assert "PRE-LOADED PROJECT DATA" in prompt
        assert "Date Range" in prompt
        assert "April 14" in prompt
        assert "April 15" in prompt

        # Address should NOT be in the prompt (removed)
        assert "123 Main St" not in prompt

        # Time slots should be in the prompt
        assert "9:00 AM" in prompt

        # Step 3 should NOT say "Show available dates"
        assert "Show available dates" not in prompt
        # Step 3 should say dates are already available
        assert "already have the available dates" in prompt
        # Step 3 should instruct summary presentation
        assert "SUMMARY" in prompt

    def test_prefetched_weather_dates_in_prompt(self):
        from channels.vapi import _build_outbound_scheduling_config

        outbound = {
            "customer_name": "Jane",
            "project_type": "Roofing",
            "prefetched": {
                "dates": {
                    "available_dates": ["2026-04-14"],
                    "dates_with_weather": [
                        ["2026-04-14", "Tue", "Sunny", 78, "[GOOD]"],
                    ],
                },
            },
        }
        config = _build_outbound_scheduling_config(
            "Hello!", "secret", outbound, "",
        )
        prompt = config["model"]["messages"][0]["content"]

        # Weather summary + recommendation
        assert "Sunny" in prompt
        assert "78°F" in prompt
        assert "Recommended Date" in prompt
        # Full weather kept in reference section
        assert "[GOOD]" in prompt

    def test_no_prefetch_uses_tool_based_flow(self):
        from channels.vapi import _build_outbound_scheduling_config

        outbound = {
            "customer_name": "Jane",
            "project_type": "Carpet",
            "project_id": "12345",
        }
        config = _build_outbound_scheduling_config(
            "Hello!", "secret", outbound, "",
        )
        prompt = config["model"]["messages"][0]["content"]

        # Without prefetched data, should use tool-based flow
        assert "PRE-LOADED PROJECT DATA" not in prompt
        assert "get_available_dates" in prompt
        # Should NOT proactively ask for address (but rule about it is fine)
        assert "Do NOT proactively ask for the installation address" in prompt
        # Project type still mentioned in the mission statement
        assert "Carpet" in prompt

    def test_no_address_confirmation_in_prompt(self):
        """Address confirmation should NOT be in the outbound prompt."""
        from channels.vapi import _build_outbound_scheduling_config

        # With prefetched data
        outbound_with = {
            "customer_name": "Jane",
            "project_type": "Tile",
            "prefetched": {
                "address": {"address1": "456 Oak Ave", "city": "Dallas", "state": "TX"},
            },
        }
        config = _build_outbound_scheduling_config("Hi!", "secret", outbound_with, "")
        prompt = config["model"]["messages"][0]["content"]
        assert "Confirm Address" not in prompt
        assert "we have your installation address" not in prompt

        # Without prefetched data
        outbound_without = {"customer_name": "Jane", "project_type": "Tile"}
        config2 = _build_outbound_scheduling_config("Hi!", "secret", outbound_without, "")
        prompt2 = config2["model"]["messages"][0]["content"]
        assert "Confirm Address" not in prompt2
        assert "Do NOT proactively ask for the installation address" in prompt2

    def test_verbal_confirmation_before_booking(self):
        """AI must summarize and get verbal YES before calling confirm_appointment."""
        from channels.vapi import _build_outbound_scheduling_config

        outbound = {
            "customer_name": "Jane",
            "project_type": "Carpet",
            "prefetched": {
                "dates": {"available_dates": [["2026-04-14", "Tue", "Sunny", "75", "GOOD"]]},
            },
        }
        config = _build_outbound_scheduling_config("Hi!", "secret", outbound, "")
        prompt = config["model"]["messages"][0]["content"]
        assert "Shall I go ahead and book that" in prompt
        assert "Wait for the customer to say YES before calling confirm_appointment" in prompt

    def test_style_rules_present(self):
        from channels.vapi import _build_outbound_scheduling_config

        outbound = {"customer_name": "Jane", "project_type": "Carpet"}
        config = _build_outbound_scheduling_config(
            "Hello!", "secret", outbound, "",
        )
        prompt = config["model"]["messages"][0]["content"]

        # Key style rules from the feedback
        assert "ONCE per action" in prompt
        assert "only discuss THIS project" in prompt.lower() or "Only discuss THIS project" in prompt
        assert "Hold on" in prompt  # prohibition mentioned
        assert "Do NOT proactively ask for the installation address" in prompt

    def test_project_scoping_in_prompt(self):
        from channels.vapi import _build_outbound_scheduling_config

        outbound = {
            "customer_name": "Jane",
            "project_type": "Balcony grill",
            "project_id": "90000120",
            "prefetched": {
                "project": {"projectNumber": "74356"},
            },
        }
        config = _build_outbound_scheduling_config(
            "Hello!", "secret", outbound, "",
        )
        prompt = config["model"]["messages"][0]["content"]

        # Should mention the project type in the mission
        assert "Balcony grill" in prompt
        assert "Do NOT mention other projects" in prompt
        # Internal project ID should NOT appear in the prompt
        assert "90000120" not in prompt
        # Direct tools — project_id is injected server-side, not in prompt
        tool_names = [t["function"]["name"] for t in config["model"]["tools"] if t.get("type") == "function"]
        assert "get_time_slots" in tool_names
        assert "confirm_appointment" in tool_names


class TestClassifyOutboundOutcome:
    def test_voicemail(self):
        from channels.vapi import _classify_outbound_outcome

        result = _classify_outbound_outcome("voicemail", "")
        assert result["status"] == "voicemail"

    def test_no_answer(self):
        from channels.vapi import _classify_outbound_outcome

        result = _classify_outbound_outcome("no-answer", "")
        assert result["status"] == "no_answer"

    def test_callback_requested(self):
        from channels.vapi import _classify_outbound_outcome

        result = _classify_outbound_outcome("customer-ended-call", "Customer said not a good time")
        assert result["status"] == "callback_requested"

    def test_completed_with_confirmation(self):
        from channels.vapi import _classify_outbound_outcome

        result = _classify_outbound_outcome("assistant-ended", "Appointment confirmed for April 10")
        assert result["status"] == "completed"

    def test_default_completed(self):
        from channels.vapi import _classify_outbound_outcome

        result = _classify_outbound_outcome("unknown", "")
        assert result["status"] == "completed"


class TestOutboundAssistantRequest:
    @pytest.mark.asyncio
    @patch("channels.vapi.get_active_call")
    @patch("channels.vapi.update_outbound_call", new_callable=AsyncMock)
    @patch("channels.vapi.get_secrets")
    async def test_outbound_detection_routes_correctly(self, mock_secrets, mock_update, mock_cache):
        """Outbound call type + our call_id in metadata triggers outbound handler."""
        from channels.vapi import _handle_assistant_request

        mock_secrets_instance = MagicMock()
        mock_secrets_instance.vapi_api_key = "test-key"
        mock_secrets.return_value = mock_secrets_instance

        mock_cache.return_value = {
            "call_id": "our-123",
            "customer_name": "Jane Doe",
            "client_name": "TestCo",
            "project_type": "Flooring",
            "auth_creds": {
                "support_number": "+15551234567",
                "timezone": "US/Eastern",
                "office_hours": [],
            },
        }

        body = {
            "message": {
                "type": "assistant-request",
                "call": {
                    "id": "vapi-call-1",
                    "type": "outboundPhoneCall",
                    "metadata": {"call_id": "our-123"},
                },
            }
        }

        result = await _handle_assistant_request(body)

        assert "assistant" in result
        config = result["assistant"]
        assert "Outbound" in config["name"]
        mock_update.assert_awaited_once()

    @pytest.mark.asyncio
    @patch("channels.vapi.get_active_call")
    @patch("channels.vapi.get_outbound_call", new_callable=AsyncMock)
    @patch("channels.vapi.update_outbound_call", new_callable=AsyncMock)
    @patch("channels.vapi.get_secrets")
    async def test_outbound_falls_back_to_dynamodb(self, mock_secrets, mock_update, mock_db, mock_cache):
        """If not in active cache, falls back to DynamoDB lookup."""
        from channels.vapi import _handle_assistant_request

        mock_secrets_instance = MagicMock()
        mock_secrets_instance.vapi_api_key = "test-key"
        mock_secrets.return_value = mock_secrets_instance

        mock_cache.return_value = None  # Not in cache
        mock_db.return_value = {
            "call_id": "our-456",
            "customer_name": "Bob",
            "client_name": "WinCo",
            "project_type": "Windows",
            "auth_creds": {"support_number": "", "timezone": "US/Eastern", "office_hours": []},
        }

        body = {
            "message": {
                "type": "assistant-request",
                "call": {
                    "id": "vapi-call-2",
                    "type": "outboundPhoneCall",
                    "metadata": {"call_id": "our-456"},
                },
            }
        }

        result = await _handle_assistant_request(body)

        assert "assistant" in result
        mock_db.assert_awaited_once_with("our-456")


class TestOutboundAuthContext:
    @pytest.mark.asyncio
    @patch("channels.vapi.get_active_call")
    async def test_outbound_creds_populate_auth_context(self, mock_cache):
        """Active outbound call creds should populate AuthContext."""
        from channels.vapi import _set_auth_context_from_phone
        from auth.context import AuthContext

        mock_cache.return_value = {
            "auth_creds": {
                "bearer_token": "outbound-jwt",
                "client_id": "outbound-client",
                "customer_id": "outbound-cust",
                "user_id": "outbound-user",
                "user_name": "Outbound Jane",
                "timezone": "US/Pacific",
                "support_number": "+15550001111",
            }
        }

        call_data = {"id": "vapi-outbound-1"}
        await _set_auth_context_from_phone(call_data, "session-1")

        assert AuthContext.get_auth_token() == "outbound-jwt"
        assert AuthContext.get_client_id() == "outbound-client"

    @pytest.mark.asyncio
    @patch("channels.vapi.cache_active_call")
    @patch("channels.vapi.get_outbound_call", new_callable=AsyncMock)
    @patch("channels.vapi.get_active_call", return_value=None)
    async def test_outbound_ddb_fallback_on_cache_miss(self, mock_cache, mock_ddb, mock_recache):
        """When in-memory cache misses, fall back to DynamoDB via metadata.call_id."""
        from channels.vapi import _set_auth_context_from_phone
        from auth.context import AuthContext

        mock_ddb.return_value = {
            "call_id": "our-internal-id",
            "auth_creds": {
                "bearer_token": "ddb-jwt",
                "client_id": "10003",
                "customer_id": "cust-ddb",
                "user_id": "user-ddb",
                "user_name": "DDB Jane",
                "timezone": "US/Eastern",
                "support_number": "+15550001111",
            }
        }

        call_data = {"id": "vapi-xyz", "metadata": {"call_id": "our-internal-id"}}
        await _set_auth_context_from_phone(call_data, "session-1")

        mock_ddb.assert_awaited_once_with("our-internal-id")
        mock_recache.assert_called_once_with("vapi-xyz", mock_ddb.return_value)
        assert AuthContext.get_auth_token() == "ddb-jwt"
        assert AuthContext.get_client_id() == "10003"


class TestOutboundDirectTools:
    """Test direct tool handling for outbound calls (bypasses orchestrator)."""

    @pytest.mark.asyncio
    @patch("channels.vapi.get_active_call")
    @patch("channels.vapi.sched_get_time_slots", new_callable=AsyncMock)
    @patch("channels.vapi.log_conversation", new_callable=AsyncMock)
    async def test_get_time_slots_injects_project_id(self, mock_log, mock_slots, mock_cache):
        from channels.vapi import _handle_outbound_direct_tool

        mock_cache.return_value = {"project_id": "90000120", "call_id": "our-1"}
        mock_slots.return_value = '{"time_slots": ["9:00 AM", "1:00 PM"]}'

        result = await _handle_outbound_direct_tool(
            "get_time_slots", {"date": "2026-04-10"}, "vapi-1", "tc-1", "sess-1", "user-1",
        )

        mock_slots.assert_awaited_once_with("90000120", "2026-04-10")
        assert "results" in result
        assert "9:00 AM" in result["results"][0]["result"]

    @pytest.mark.asyncio
    @patch("channels.vapi.get_active_call")
    @patch("channels.vapi.sched_confirm_appointment", new_callable=AsyncMock)
    @patch("channels.vapi.log_conversation", new_callable=AsyncMock)
    async def test_confirm_appointment_injects_project_id(self, mock_log, mock_confirm, mock_cache):
        from channels.vapi import _handle_outbound_direct_tool

        mock_cache.return_value = {"project_id": "90000120", "call_id": "our-1"}
        mock_confirm.return_value = "Appointment confirmed! Project 90000120 is scheduled."

        result = await _handle_outbound_direct_tool(
            "confirm_appointment", {"date": "2026-04-10", "time": "9:00 AM"},
            "vapi-1", "tc-1", "sess-1", "user-1",
        )

        mock_confirm.assert_awaited_once_with("90000120", "2026-04-10", "9:00 AM")
        assert "confirmed" in result["results"][0]["result"].lower()

    @pytest.mark.asyncio
    @patch("channels.vapi.get_active_call")
    @patch("channels.vapi.sched_add_note", new_callable=AsyncMock)
    @patch("channels.vapi.log_conversation", new_callable=AsyncMock)
    async def test_add_note_injects_project_id(self, mock_log, mock_note, mock_cache):
        from channels.vapi import _handle_outbound_direct_tool

        mock_cache.return_value = {"project_id": "90000120", "call_id": "our-1"}
        mock_note.return_value = "Note added successfully to project 90000120."

        result = await _handle_outbound_direct_tool(
            "add_note", {"note_text": "ADDRESS CORRECTION: 123 New St"},
            "vapi-1", "tc-1", "sess-1", "user-1",
        )

        mock_note.assert_awaited_once_with("90000120", "ADDRESS CORRECTION: 123 New St")
        assert "Note added" in result["results"][0]["result"]

    @pytest.mark.asyncio
    @patch("channels.vapi.get_active_call")
    async def test_no_active_call_returns_error(self, mock_cache):
        from channels.vapi import _handle_outbound_direct_tool

        mock_cache.return_value = None

        result = await _handle_outbound_direct_tool(
            "get_time_slots", {"date": "2026-04-10"}, "vapi-1", "tc-1", "sess-1", "user-1",
        )

        assert "trouble" in result["results"][0]["result"].lower()

    @pytest.mark.asyncio
    @patch("channels.vapi.get_active_call")
    @patch("channels.vapi.sched_get_time_slots", new_callable=AsyncMock)
    @patch("channels.vapi.log_conversation", new_callable=AsyncMock)
    async def test_tool_exception_returns_graceful_error(self, mock_log, mock_slots, mock_cache):
        from channels.vapi import _handle_outbound_direct_tool

        mock_cache.return_value = {"project_id": "90000120", "call_id": "our-1"}
        mock_slots.side_effect = Exception("API timeout")

        result = await _handle_outbound_direct_tool(
            "get_time_slots", {"date": "2026-04-10"}, "vapi-1", "tc-1", "sess-1", "user-1",
        )

        assert "trouble" in result["results"][0]["result"].lower()

    def test_outbound_tools_list_with_prefetch(self):
        """When dates are pre-fetched, get_available_dates should NOT be a tool."""
        from channels.vapi import _outbound_scheduling_tools

        tools = _outbound_scheduling_tools("+15551234567", "TestCo", has_dates=True)
        fn_names = [t["function"]["name"] for t in tools if t.get("type") == "function"]

        assert "get_time_slots" in fn_names
        assert "confirm_appointment" in fn_names
        assert "add_note" in fn_names
        assert "get_available_dates" not in fn_names

    def test_outbound_tools_list_without_prefetch(self):
        """When dates are NOT pre-fetched, get_available_dates should be included."""
        from channels.vapi import _outbound_scheduling_tools

        tools = _outbound_scheduling_tools("+15551234567", "TestCo", has_dates=False)
        fn_names = [t["function"]["name"] for t in tools if t.get("type") == "function"]

        assert "get_time_slots" in fn_names
        assert "confirm_appointment" in fn_names
        assert "add_note" in fn_names
        assert "get_available_dates" in fn_names
