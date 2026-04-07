"""Tests for outbound call endpoints."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from channels.vapi import verify_vapi_secret
from main import app


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def authed_client():
    """TestClient with vapi auth dependency overridden."""
    async def _noop(request=None):
        pass

    app.dependency_overrides[verify_vapi_secret] = _noop
    yield TestClient(app)
    app.dependency_overrides.pop(verify_vapi_secret, None)


class TestGetStatus:
    @patch("channels.outbound.get_outbound_call", new_callable=AsyncMock)
    def test_returns_call_status(self, mock_get, client):
        mock_get.return_value = {
            "call_id": "call-123",
            "project_id": "proj-456",
            "status": "completed",
            "attempt_number": 1,
            "phone_used": "+15551234567",
            "vapi_call_id": "vapi-abc",
            "call_result": {"outcome": "scheduled"},
            "created_at": "2026-04-07T10:00:00Z",
            "updated_at": "2026-04-07T10:05:00Z",
        }

        resp = client.get("/outbound/call-123/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["call_id"] == "call-123"
        assert body["project_id"] == "proj-456"
        assert body["status"] == "completed"
        assert body["attempt_number"] == 1
        assert body["phone_used"] == "+15551234567"
        assert body["vapi_call_id"] == "vapi-abc"
        assert body["call_result"] == {"outcome": "scheduled"}
        assert body["created_at"] == "2026-04-07T10:00:00Z"
        assert body["updated_at"] == "2026-04-07T10:05:00Z"

    @patch("channels.outbound.get_outbound_call", new_callable=AsyncMock)
    def test_returns_404_when_not_found(self, mock_get, client):
        mock_get.return_value = None

        resp = client.get("/outbound/nonexistent/status")

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Call not found"

    @patch("channels.outbound.get_outbound_call", new_callable=AsyncMock)
    def test_missing_optional_fields_use_defaults(self, mock_get, client):
        """Fields missing from DynamoDB record should default gracefully."""
        mock_get.return_value = {
            "call_id": "call-minimal",
        }

        resp = client.get("/outbound/call-minimal/status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["call_id"] == "call-minimal"
        assert body["project_id"] == ""
        assert body["status"] == "unknown"
        assert body["attempt_number"] == 0
        assert body["phone_used"] == ""
        assert body["vapi_call_id"] == ""
        assert body["call_result"] is None
        assert body["created_at"] == ""
        assert body["updated_at"] == ""


class TestListCalls:
    @patch("channels.outbound.get_calls_for_project", new_callable=AsyncMock)
    def test_returns_calls_for_project(self, mock_query, client):
        mock_query.return_value = [
            {"call_id": "c1", "status": "completed"},
            {"call_id": "c2", "status": "pending"},
        ]

        resp = client.get("/outbound/calls?project_id=proj-456")

        assert resp.status_code == 200
        body = resp.json()
        assert body["project_id"] == "proj-456"
        assert body["count"] == 2
        assert len(body["calls"]) == 2
        mock_query.assert_awaited_once_with("proj-456")

    @patch("channels.outbound.get_calls_for_project", new_callable=AsyncMock)
    def test_empty_project_returns_empty_with_message(self, mock_query, client):
        """No project_id param -> returns empty list with helpful message."""
        resp = client.get("/outbound/calls")

        assert resp.status_code == 200
        body = resp.json()
        assert body["calls"] == []
        assert "project_id" in body["message"].lower()
        mock_query.assert_not_awaited()

    @patch("channels.outbound.get_calls_for_project", new_callable=AsyncMock)
    def test_no_calls_found(self, mock_query, client):
        mock_query.return_value = []

        resp = client.get("/outbound/calls?project_id=proj-empty")

        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0
        assert body["calls"] == []


class TestManualTrigger:
    """Manual trigger requires x-vapi-secret and calls process_trigger."""

    @patch("channels.outbound_consumer.process_trigger", new_callable=AsyncMock)
    def test_valid_trigger_initiates_call(self, mock_trigger, authed_client):
        mock_trigger.return_value = {
            "call_id": "call-new-123",
            "vapi_call_id": "vapi-new-abc",
            "status": "calling",
        }

        resp = authed_client.post(
            "/outbound/trigger",
            json={
                "project_id": "proj-789",
                "client_id": "client-abc",
                "customer_phone": "+15551234567",
            },
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["call_id"] == "call-new-123"
        assert body["vapi_call_id"] == "vapi-new-abc"
        assert body["status"] == "calling"
        mock_trigger.assert_awaited_once()

    def test_trigger_missing_required_fields(self, authed_client):
        """Missing project_id or customer_phone -> 422."""
        resp = authed_client.post(
            "/outbound/trigger",
            json={"project_id": "proj-789"},
        )
        assert resp.status_code == 422

    def test_trigger_empty_body(self, authed_client):
        resp = authed_client.post(
            "/outbound/trigger",
            json={},
        )
        assert resp.status_code == 422

    def test_trigger_without_auth_returns_401(self, client):
        """No x-vapi-secret header -> 401."""
        resp = client.post(
            "/outbound/trigger",
            json={
                "project_id": "proj-789",
                "client_id": "client-abc",
                "customer_phone": "+15551234567",
            },
        )
        assert resp.status_code == 401

    @patch("channels.outbound_consumer.process_trigger", new_callable=AsyncMock)
    def test_trigger_failure_returns_500(self, mock_trigger, authed_client):
        mock_trigger.side_effect = Exception("Vapi call failed")

        resp = authed_client.post(
            "/outbound/trigger",
            json={
                "project_id": "proj-789",
                "client_id": "client-abc",
                "customer_phone": "+15551234567",
            },
        )

        assert resp.status_code == 500
