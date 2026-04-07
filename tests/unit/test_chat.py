"""Tests for chat channel endpoints."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture()
def client():
    return TestClient(app)


class TestHealth:
    def test_health_check(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"
        assert body["service"] == "scheduling-bot"


class TestChat:
    @patch("channels.chat.get_orchestrator")
    def test_chat_success(self, mock_get_orch, client):
        mock_response = MagicMock()
        mock_response.output = "Your project is scheduled for March 15."
        mock_response.metadata = MagicMock()
        mock_response.metadata.agent_name = "Scheduling Agent"

        mock_orch = AsyncMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        mock_get_orch.return_value = mock_orch

        resp = client.post("/chat", json={
            "message": "Show my projects",
            "client_id": "test-client",
            "customer_id": "test-customer",
        })

        assert resp.status_code == 200
        body = resp.json()
        assert body["response"] == "Your project is scheduled for March 15."
        assert body["agent_name"] == "Scheduling Agent"
        assert body["session_id"]  # Should have a session ID

    @patch("channels.chat.get_orchestrator")
    def test_chat_response_has_v129_fields(self, mock_get_orch, client):
        """Response must include all v1.2.9 fields for frontend compatibility."""
        mock_response = MagicMock()
        mock_response.output = "Here are your projects."
        mock_response.metadata = MagicMock()
        mock_response.metadata.agent_name = "Scheduling Agent"

        mock_orch = AsyncMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        mock_get_orch.return_value = mock_orch

        resp = client.post("/chat", json={"message": "Show projects"})
        body = resp.json()

        # All v1.2.9 fields must be present
        assert "response" in body
        assert "session_id" in body
        assert "agent_name" in body
        assert "intent" in body
        assert "pf_http_status_code" in body
        assert "agenticscheduler_http_status_code" in body

        # Correct default values
        assert body["pf_http_status_code"] == 200
        assert body["agenticscheduler_http_status_code"] == 200
        assert body["intent"] == "scheduling"

    @patch("channels.chat.get_orchestrator")
    def test_chat_chitchat_intent(self, mock_get_orch, client):
        mock_response = MagicMock()
        mock_response.output = "Hello! How can I help?"
        mock_response.metadata = MagicMock()
        mock_response.metadata.agent_name = "Chitchat Agent"

        mock_orch = AsyncMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        mock_get_orch.return_value = mock_orch

        resp = client.post("/chat", json={"message": "hi"})
        assert resp.json()["intent"] == "chitchat"

    @patch("channels.chat.get_orchestrator")
    def test_chat_with_session_continuity(self, mock_get_orch, client):
        mock_response = MagicMock()
        mock_response.output = "Response"
        mock_response.metadata = MagicMock()
        mock_response.metadata.agent_name = "Scheduling Agent"

        mock_orch = AsyncMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        mock_get_orch.return_value = mock_orch

        resp = client.post("/chat", json={
            "message": "Schedule it",
            "session_id": "existing-session-123",
        })

        assert resp.status_code == 200
        assert resp.json()["session_id"] == "existing-session-123"

    def test_chat_empty_message_rejected(self, client):
        resp = client.post("/chat", json={"message": ""})
        assert resp.status_code == 422  # Validation error

    @patch("channels.chat.get_orchestrator")
    def test_chat_orchestrator_error_returns_v129_error(self, mock_get_orch, client):
        """Error response must match v1.2.9 shape (error + status codes)."""
        mock_orch = AsyncMock()
        mock_orch.route_request = AsyncMock(side_effect=RuntimeError("Bedrock timeout"))
        mock_get_orch.return_value = mock_orch

        resp = client.post("/chat", json={"message": "Hello"})
        assert resp.status_code == 500
        body = resp.json()
        assert "error" in body
        assert body["agenticscheduler_http_status_code"] == 500

    @patch("channels.chat.get_orchestrator")
    def test_auth_expired_detected(self, mock_get_orch, client):
        """401 from PF API detected in response text → pf_http_status_code=401."""
        mock_response = MagicMock()
        mock_response.output = "Authentication expired. Please log in again."
        mock_response.metadata = MagicMock()
        mock_response.metadata.agent_name = "Scheduling Agent"

        mock_orch = AsyncMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        mock_get_orch.return_value = mock_orch

        resp = client.post("/chat", json={"message": "Show projects"})
        assert resp.json()["pf_http_status_code"] == 401


class TestWelcome:
    @patch("channels.chat._store_welcome_in_history", new_callable=AsyncMock)
    @patch("channels.chat.handle_welcome", new_callable=AsyncMock)
    def test_welcome_returns_greeting(self, mock_welcome, mock_store, client):
        mock_welcome.return_value = {
            "response": "Hey John! You've got 2 projects.",
            "agent_name": "Welcome",
            "projects": [{"id": "1"}, {"id": "2"}],
        }

        resp = client.post("/chat", json={
            "message": "__WELCOME__",
            "user_name": "John",
            "client_id": "c1",
            "customer_id": "cust1",
        })

        assert resp.status_code == 200
        body = resp.json()
        assert body["agent_name"] == "Welcome"
        assert "John" in body["response"]
        mock_welcome.assert_awaited_once_with(user_name="John")
        mock_store.assert_awaited_once()

    @patch("channels.chat._store_welcome_in_history", new_callable=AsyncMock)
    @patch("channels.chat.handle_welcome", new_callable=AsyncMock)
    def test_welcome_has_v129_fields(self, mock_welcome, mock_store, client):
        """Welcome response must include all v1.2.9 fields."""
        mock_welcome.return_value = {
            "response": "Hey!",
            "agent_name": "Welcome",
            "projects": [{"id": "1", "status": "New"}],
        }

        resp = client.post("/chat", json={
            "message": "__WELCOME__",
            "client_id": "c1",
            "customer_id": "cust1",
        })

        body = resp.json()
        assert body["intent"] == "welcome"
        assert body["action"] == "welcome_with_projects"
        assert body["pf_http_status_code"] == 200
        assert body["agenticscheduler_http_status_code"] == 200
        assert body["projects"] == [{"id": "1", "status": "New"}]

    @patch("channels.chat._store_welcome_in_history", new_callable=AsyncMock)
    @patch("channels.chat.handle_welcome", new_callable=AsyncMock)
    def test_welcome_stream_returns_sse(self, mock_welcome, mock_store, client):
        mock_welcome.return_value = {
            "response": "Hey! You've got 1 project.",
            "agent_name": "Welcome",
            "projects": [{"id": "1"}],
        }

        resp = client.post("/chat/stream", json={
            "message": "__WELCOME__",
            "client_id": "c1",
            "customer_id": "cust1",
        })

        assert resp.status_code == 200
        body = resp.text
        assert "event: delta" in body
        assert "event: done" in body
        assert "You've got 1 project" in body
        # done event should contain v1.2.9 metadata
        assert '"intent": "welcome"' in body
        assert '"pf_http_status_code": 200' in body

    @patch("channels.chat.handle_welcome", new_callable=AsyncMock)
    def test_welcome_error_returns_v129_error(self, mock_welcome, client):
        mock_welcome.side_effect = RuntimeError("Bedrock down")

        resp = client.post("/chat", json={
            "message": "__WELCOME__",
            "client_id": "c1",
        })
        assert resp.status_code == 500
        body = resp.json()
        assert "error" in body
        assert body["agenticscheduler_http_status_code"] == 500

    @patch("channels.chat._store_welcome_in_history", new_callable=AsyncMock)
    @patch("channels.chat.handle_welcome", new_callable=AsyncMock)
    def test_welcome_preserves_session_id(self, mock_welcome, mock_store, client):
        mock_welcome.return_value = {
            "response": "Hey!",
            "agent_name": "Welcome",
            "projects": [],
        }

        resp = client.post("/chat", json={
            "message": "__WELCOME__",
            "session_id": "my-session-123",
        })

        assert resp.status_code == 200
        assert resp.json()["session_id"] == "my-session-123"


class TestChatStream:
    @patch("channels.chat.get_orchestrator")
    def test_stream_returns_sse(self, mock_get_orch, client):
        mock_response = MagicMock()
        mock_response.streaming = False
        mock_response.output = "Streamed answer"
        mock_response.metadata = MagicMock()
        mock_response.metadata.agent_name = "Scheduling Agent"

        mock_orch = AsyncMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        mock_get_orch.return_value = mock_orch

        resp = client.post("/chat/stream", json={"message": "Hello"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = resp.text
        assert "event: delta" in body
        assert "event: done" in body

    @patch("channels.chat.get_orchestrator")
    def test_stream_done_event_has_v129_metadata(self, mock_get_orch, client):
        """SSE done event must contain v1.2.9 metadata fields."""
        mock_response = MagicMock()
        mock_response.streaming = False
        mock_response.output = "Answer text"
        mock_response.metadata = MagicMock()
        mock_response.metadata.agent_name = "Scheduling Agent"

        mock_orch = AsyncMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        mock_get_orch.return_value = mock_orch

        resp = client.post("/chat/stream", json={"message": "Show projects"})
        body = resp.text
        assert '"pf_http_status_code": 200' in body
        assert '"agenticscheduler_http_status_code": 200' in body
        assert '"intent": "scheduling"' in body


class TestDetectResponseSignals:
    """confirmation_required comes from the LLM's JSON block, not regex."""

    def test_scheduling_confirmation_detected(self):
        from channels.chat import _detect_response_signals

        text = (
            'Great! Should I go ahead and schedule your carpet installation '
            'for April 10th at 9:00 AM?\n\n'
            '```json\n'
            '{"message": "Confirm appointment", "confirmation_required": true, '
            '"project_id": "90000119", "project_type": "Windows Installation", '
            '"date": "2026-04-10", "time": "13:00:00", "display_time": "1:00 PM", '
            '"address": "123 Main St"}\n'
            '```'
        )
        signals = _detect_response_signals(text)
        assert signals["confirmation_required"] is True
        assert signals["action"] == "confirm_appointment_preview"
        pa = signals["pending_action"]
        assert pa["project_id"] == "90000119"
        assert pa["project_name"] == "Windows"
        assert pa["project_type"] == "Installation"
        assert pa["rawDate"] == "2026-04-10"
        assert pa["date"] == "Fri 04/10/2026"
        assert pa["time"] == "13:00"
        assert pa["formattedTime"] == "1:00 PM"
        assert pa["address"] == "123 Main St"

    def test_reschedule_confirmation_detected(self):
        from channels.chat import _detect_response_signals

        text = (
            'Would you like me to reschedule to April 15th at 1:00 PM?\n\n'
            '```json\n'
            '{"message": "Reschedule appointment", "confirmation_required": true}\n'
            '```'
        )
        signals = _detect_response_signals(text)
        assert signals["confirmation_required"] is True

    def test_cancel_confirmation_detected(self):
        from channels.chat import _detect_response_signals

        text = (
            'Are you sure you want to cancel your windows appointment?\n\n'
            '```json\n'
            '{"message": "Cancel appointment", "confirmation_required": true}\n'
            '```'
        )
        signals = _detect_response_signals(text)
        assert signals["confirmation_required"] is True

    def test_list_projects_confirmation_false(self):
        from channels.chat import _detect_response_signals

        text = (
            'You have 3 projects! Here they are:\n\n'
            '```json\n'
            '{"message": "Found 3 project(s):", "projects": [{"id": "123"}], '
            '"confirmation_required": false}\n'
            '```'
        )
        signals = _detect_response_signals(text)
        assert signals["confirmation_required"] is False

    def test_project_details_confirmation_false(self):
        from channels.chat import _detect_response_signals

        text = (
            'Here are the details for your flooring project:\n\n'
            '```json\n'
            '{"message": "Project details", "project": {"id": "456"}, '
            '"confirmation_required": false}\n'
            '```'
        )
        signals = _detect_response_signals(text)
        assert signals["confirmation_required"] is False

    def test_available_dates_confirmation_false(self):
        from channels.chat import _detect_response_signals

        text = (
            'Here are the available dates:\n\n'
            '```json\n'
            '{"available_dates": ["2026-04-10"], "confirmation_required": false}\n'
            '```'
        )
        signals = _detect_response_signals(text)
        assert signals["confirmation_required"] is False

    def test_time_slots_confirmation_false(self):
        from channels.chat import _detect_response_signals

        text = (
            'Here are the available time slots for April 10th:\n\n'
            '```json\n'
            '{"time_slots": ["9:00 AM", "1:00 PM"], "confirmation_required": false}\n'
            '```'
        )
        signals = _detect_response_signals(text)
        assert signals["confirmation_required"] is False

    def test_no_json_block_defaults_false(self):
        from channels.chat import _detect_response_signals

        text = "Should I go ahead and schedule this for you?"
        signals = _detect_response_signals(text)
        assert signals["confirmation_required"] is False

    def test_missing_field_defaults_false(self):
        from channels.chat import _detect_response_signals

        text = (
            '```json\n'
            '{"message": "Projects listed"}\n'
            '```'
        )
        signals = _detect_response_signals(text)
        assert signals["confirmation_required"] is False

    def test_auth_failure_detected(self):
        from channels.chat import _detect_response_signals

        text = "Your authentication expired. Please log in again."
        signals = _detect_response_signals(text)
        assert signals["pf_http_status_code"] == 401


class TestGroupTimeSlots:
    def test_am_slots_go_to_morning(self):
        from channels.chat import _group_time_slots

        result = _group_time_slots(["8:00 AM", "9:00 AM", "10:00 AM"])
        assert result["morning"]["slots"] == ["8:00 AM", "9:00 AM", "10:00 AM"]
        assert result["morning"]["count"] == 3
        assert result["afternoon"]["count"] == 0
        assert result["evening"]["count"] == 0

    def test_pm_slots_grouped_correctly(self):
        from channels.chat import _group_time_slots

        result = _group_time_slots(["12:00 PM", "1:00 PM", "3:00 PM", "5:00 PM", "6:00 PM"])
        assert result["afternoon"]["slots"] == ["12:00 PM", "1:00 PM", "3:00 PM"]
        assert result["evening"]["slots"] == ["5:00 PM", "6:00 PM"]

    def test_mixed_am_pm(self):
        from channels.chat import _group_time_slots

        result = _group_time_slots(["8:00 AM", "1:00 PM"])
        assert result["morning"]["slots"] == ["8:00 AM"]
        assert result["afternoon"]["slots"] == ["1:00 PM"]

    def test_empty_list(self):
        from channels.chat import _group_time_slots

        result = _group_time_slots([])
        assert result["morning"]["count"] == 0
        assert result["afternoon"]["count"] == 0
        assert result["evening"]["count"] == 0


class TestEnrichJsonBlock:
    def test_adds_grouped_slots_from_time_slots_key(self):
        from channels.chat import _enrich_json_block

        text = (
            'Here are the available times:\n\n'
            '```json\n'
            '{"time_slots": ["8:00 AM", "1:00 PM"], "confirmation_required": false}\n'
            '```'
        )
        result = _enrich_json_block(text)
        assert '"timeSlotsGrouped"' in result
        assert '"slotCount": 2' in result

    def test_adds_grouped_slots_from_timeSlots_key(self):
        from channels.chat import _enrich_json_block

        text = (
            '```json\n'
            '{"timeSlots": ["9:00 AM", "2:00 PM", "5:00 PM"]}\n'
            '```'
        )
        result = _enrich_json_block(text)
        assert '"timeSlotsGrouped"' in result
        assert '"slotCount": 3' in result

    def test_handles_dict_slot_objects(self):
        from channels.chat import _enrich_json_block
        import json

        text = (
            '```json\n'
            '{"time_slots": [{"time": "08:00", "display_time": "8:00 AM"}, '
            '{"time": "13:00", "display_time": "1:00 PM"}]}\n'
            '```'
        )
        result = _enrich_json_block(text)
        # Parse the enriched JSON block
        import re
        match = re.search(r'```json\s*\n(.*?)```', result, re.DOTALL)
        data = json.loads(match.group(1))
        assert data["timeSlots"] == ["8:00 AM", "1:00 PM"]
        assert data["slotCount"] == 2
        assert data["timeSlotsGrouped"]["morning"]["slots"] == ["8:00 AM"]
        assert data["timeSlotsGrouped"]["afternoon"]["slots"] == ["1:00 PM"]

    def test_available_slots_key_detected(self):
        """LLM sometimes uses 'available_slots' instead of 'time_slots'."""
        from channels.chat import _enrich_json_block
        import json
        import re

        text = (
            '```json\n'
            '{"project_id": "90000119", "date": "2026-04-16", '
            '"available_slots": [{"time": "08:00:00", "display_time": "8:00 AM"}, '
            '{"time": "13:00:00", "display_time": "1:00 PM"}], '
            '"message": "Found 2 available time slot(s)"}\n'
            '```'
        )
        result = _enrich_json_block(text)
        match = re.search(r'```json\s*\n(.*?)```', result, re.DOTALL)
        data = json.loads(match.group(1))
        assert data["timeSlots"] == ["8:00 AM", "1:00 PM"]
        assert data["slotCount"] == 2
        assert data["timeSlotsGrouped"]["morning"]["slots"] == ["8:00 AM"]
        assert data["timeSlotsGrouped"]["afternoon"]["slots"] == ["1:00 PM"]

    def test_available_time_slots_key_detected(self):
        """LLM sometimes uses 'available_time_slots'."""
        from channels.chat import _enrich_json_block

        text = (
            '```json\n'
            '{"available_time_slots": ["9:00 AM", "2:00 PM"]}\n'
            '```'
        )
        result = _enrich_json_block(text)
        assert '"timeSlotsGrouped"' in result
        assert '"slotCount": 2' in result

    def test_no_json_block_returns_unchanged(self):
        from channels.chat import _enrich_json_block

        text = "No slots available today."
        assert _enrich_json_block(text) == text

    def test_no_time_slots_returns_unchanged(self):
        from channels.chat import _enrich_json_block

        text = (
            '```json\n'
            '{"projects": [{"id": "123"}], "confirmation_required": false}\n'
            '```'
        )
        assert _enrich_json_block(text) == text

    def test_already_grouped_returns_unchanged(self):
        from channels.chat import _enrich_json_block

        text = (
            '```json\n'
            '{"time_slots": ["8:00 AM"], "timeSlotsGrouped": {"morning": {}}}\n'
            '```'
        )
        assert _enrich_json_block(text) == text

    def test_invalid_json_returns_unchanged(self):
        from channels.chat import _enrich_json_block

        text = '```json\n{broken json\n```'
        assert _enrich_json_block(text) == text
