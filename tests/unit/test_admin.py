"""Tests for admin endpoints."""

from unittest.mock import patch

import pytest

from channels.admin import router


class TestVapiAssistantEndpoints:
    """Tests for /admin/vapi-assistants endpoints."""

    def test_list_empty(self):
        """Returns empty list when no assistants registered."""
        with patch("channels.admin.list_assistants", return_value=[]):
            from fastapi.testclient import TestClient

            from main import app

            client = TestClient(app)
            resp = client.get("/admin/vapi-assistants")

        assert resp.status_code == 200
        assert resp.json()["count"] == 0
        assert resp.json()["assistants"] == []

    def test_register_assistant(self):
        """Registers a new assistant."""
        with patch("channels.admin.register_assistant") as mock_reg:
            from fastapi.testclient import TestClient

            from main import app

            client = TestClient(app)
            resp = client.post(
                "/admin/vapi-assistants",
                json={
                    "assistant_id": "asst-123",
                    "phone_number": "+19566699322",
                    "tenant_name": "Test Corp",
                },
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["assistant_id"] == "asst-123"
        assert data["phone_number"] == "+19566699322"
        assert data["tenant_name"] == "Test Corp"
        mock_reg.assert_called_once_with("asst-123", "+19566699322", "Test Corp")

    def test_register_missing_fields(self):
        """Rejects registration without required fields."""
        from fastapi.testclient import TestClient

        from main import app

        client = TestClient(app)
        resp = client.post(
            "/admin/vapi-assistants",
            json={"assistant_id": "", "phone_number": "+19566699322"},
        )

        assert resp.status_code == 400

    def test_delete_assistant(self):
        """Deletes an assistant config."""
        with patch("channels.admin.delete_assistant", return_value=True):
            from fastapi.testclient import TestClient

            from main import app

            client = TestClient(app)
            resp = client.delete("/admin/vapi-assistants/asst-123")

        assert resp.status_code == 200
        assert resp.json()["deleted"] == "asst-123"

    def test_delete_not_found(self):
        """Returns 404 when assistant not found."""
        with patch("channels.admin.delete_assistant", return_value=False):
            from fastapi.testclient import TestClient

            from main import app

            client = TestClient(app)
            resp = client.delete("/admin/vapi-assistants/unknown")

        assert resp.status_code == 404
