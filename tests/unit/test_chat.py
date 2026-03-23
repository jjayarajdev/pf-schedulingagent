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
