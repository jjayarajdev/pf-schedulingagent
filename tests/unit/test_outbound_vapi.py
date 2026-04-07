"""Tests for outbound_vapi.py — Vapi API client."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from channels.outbound_vapi import create_vapi_call, get_vapi_call_status


@pytest.fixture()
def mock_secrets():
    with patch("channels.outbound_vapi.get_secrets") as mock:
        secrets = MagicMock()
        secrets.vapi_api_key = "test-vapi-key"
        mock.return_value = secrets
        yield mock


class TestCreateVapiCall:
    @pytest.mark.asyncio
    async def test_sends_correct_payload(self, mock_secrets):
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": "vapi-call-123", "status": "queued"}
        mock_response.text = '{"id": "vapi-call-123"}'
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            result = await create_vapi_call(
                phone_number_id="ph-001",
                customer_phone="+15551234567",
                customer_name="Jane Doe",
                server_url="https://bot.example.com/vapi/webhook",
                metadata={"call_id": "our-123"},
            )

        assert result["id"] == "vapi-call-123"
        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]
        assert payload["phoneNumberId"] == "ph-001"
        assert payload["customer"]["number"] == "+15551234567"
        assert payload["customer"]["name"] == "Jane Doe"
        assert payload["serverUrl"] == "https://bot.example.com/vapi/webhook"
        assert payload["metadata"]["call_id"] == "our-123"

        headers = call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer test-vapi-key"

    @pytest.mark.asyncio
    async def test_raises_on_error(self, mock_secrets):
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Bad Request"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=mock_response,
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            with pytest.raises(httpx.HTTPStatusError):
                await create_vapi_call(
                    phone_number_id="ph-001",
                    customer_phone="+15551234567",
                    customer_name="Jane",
                    server_url="https://bot.example.com/vapi/webhook",
                )

    @pytest.mark.asyncio
    async def test_omits_metadata_when_none(self, mock_secrets):
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": "v1"}
        mock_response.text = '{"id": "v1"}'
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            await create_vapi_call(
                phone_number_id="ph-001",
                customer_phone="+15551234567",
                customer_name="Jane",
                server_url="https://bot.example.com/vapi/webhook",
            )

        payload = mock_client.post.call_args[1]["json"]
        assert "metadata" not in payload


class TestGetVapiCallStatus:
    @pytest.mark.asyncio
    async def test_returns_status(self, mock_secrets):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "v1", "status": "ended"}
        mock_response.text = '{"id": "v1", "status": "ended"}'
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            result = await get_vapi_call_status("v1")

        assert result["status"] == "ended"
        url = mock_client.get.call_args[0][0]
        assert "v1" in url
