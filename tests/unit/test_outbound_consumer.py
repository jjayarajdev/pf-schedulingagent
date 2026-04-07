"""Tests for outbound_consumer.py — SQS consumer and message processing."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from channels.outbound_consumer import (
    _process_outbound_message,
    process_trigger,
    retry_outbound_call,
)


@pytest.fixture()
def mock_sqs():
    return MagicMock()


@pytest.fixture()
def mock_auth_creds():
    return {
        "bearer_token": "test-jwt",
        "client_id": "client-1",
        "customer_id": "cust-1",
        "user_id": "user-1",
        "user_name": "Jane Doe",
        "timezone": "US/Eastern",
        "support_number": "+15559999999",
    }


def _make_sqs_message(body: dict, message_id: str = "msg-1") -> dict:
    return {
        "MessageId": message_id,
        "ReceiptHandle": "receipt-123",
        "Body": json.dumps(body),
    }


# ── Message processing tests ──────────────────────────────────────


class TestProcessOutboundMessage:
    @pytest.mark.asyncio
    @patch("channels.outbound_consumer.cache_active_call")
    @patch("channels.outbound_consumer.update_outbound_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.create_outbound_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.create_vapi_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.get_or_authenticate", new_callable=AsyncMock)
    async def test_happy_path(
        self, mock_auth, mock_vapi, mock_create, mock_update, mock_cache, mock_sqs
    ):
        mock_auth.return_value = {
            "bearer_token": "jwt", "client_id": "c1", "customer_id": "cu1",
            "user_id": "u1", "user_name": "Jane", "client_name": "TestCo",
            "timezone": "US/Eastern", "support_number": "+15559999999",
        }
        mock_create.return_value = "call-001"
        mock_vapi.return_value = {"id": "vapi-abc"}

        msg = _make_sqs_message({
            "project_id": "proj-1",
            "client_id": "client-1",
            "customer_phone": "+15551234567",
            "customer_name": "Jane Doe",
            "project_type": "Flooring",
        })

        await _process_outbound_message(msg, mock_sqs, "https://queue-url")

        mock_auth.assert_awaited_once_with("+15551234567")
        mock_create.assert_awaited_once()
        mock_vapi.assert_awaited_once()
        mock_update.assert_awaited_once()
        mock_cache.assert_called_once()
        mock_sqs.delete_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_json_deletes_message(self, mock_sqs):
        msg = {
            "MessageId": "msg-bad",
            "ReceiptHandle": "receipt-bad",
            "Body": "not json {{{",
        }

        await _process_outbound_message(msg, mock_sqs, "https://queue-url")

        mock_sqs.delete_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_required_fields_deletes_message(self, mock_sqs):
        msg = _make_sqs_message({"project_id": ""})

        await _process_outbound_message(msg, mock_sqs, "https://queue-url")

        mock_sqs.delete_message.assert_called_once()

    @pytest.mark.asyncio
    @patch("channels.outbound_consumer.create_outbound_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.get_or_authenticate", new_callable=AsyncMock)
    async def test_auth_failure_creates_failed_record(
        self, mock_auth, mock_create, mock_sqs
    ):
        from auth.phone_auth import AuthenticationError

        mock_auth.side_effect = AuthenticationError("auth failed")
        mock_create.return_value = "call-fail"

        msg = _make_sqs_message({
            "project_id": "proj-1",
            "client_id": "c1",
            "customer_phone": "+15551234567",
        })

        await _process_outbound_message(msg, mock_sqs, "https://queue-url")

        mock_create.assert_awaited_once()
        create_data = mock_create.call_args[0][0]
        assert create_data["status"] == "failed"
        mock_sqs.delete_message.assert_called_once()


# ── Retry tests ───────────────────────────────────────────────────


class TestRetryOutboundCall:
    @pytest.mark.asyncio
    @patch("channels.outbound_consumer.cache_active_call")
    @patch("channels.outbound_consumer.update_outbound_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.create_vapi_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.get_or_authenticate", new_callable=AsyncMock)
    async def test_retries_on_alternate_number(
        self, mock_auth, mock_vapi, mock_update, mock_cache
    ):
        mock_auth.return_value = {
            "bearer_token": "jwt2", "client_id": "c1", "customer_id": "cu1",
            "user_id": "u1", "user_name": "Jane",
            "timezone": "US/Eastern", "support_number": "",
        }
        mock_vapi.return_value = {"id": "vapi-retry-1"}

        outbound = {
            "call_id": "c1",
            "attempt_number": 1,
            "max_attempts": 2,
            "phone_alternate": "+15559876543",
            "customer_name": "Jane",
            "project_id": "p1",
            "vapi_phone_number_id": "ph-001",
        }

        await retry_outbound_call(outbound)

        mock_auth.assert_awaited_once_with("+15559876543")
        mock_vapi.assert_awaited_once()
        assert mock_update.await_count == 2  # status=calling, then vapi_call_id
        mock_cache.assert_called_once()

    @pytest.mark.asyncio
    @patch("channels.outbound_consumer.update_outbound_call", new_callable=AsyncMock)
    async def test_no_retry_at_max_attempts(self, mock_update):
        outbound = {
            "call_id": "c1",
            "attempt_number": 2,
            "max_attempts": 2,
            "phone_alternate": "+15559876543",
        }

        await retry_outbound_call(outbound)

        mock_update.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("channels.outbound_consumer.update_outbound_call", new_callable=AsyncMock)
    async def test_no_retry_without_alternate(self, mock_update):
        outbound = {
            "call_id": "c1",
            "attempt_number": 1,
            "max_attempts": 2,
            "phone_alternate": "",
        }

        await retry_outbound_call(outbound)

        mock_update.assert_not_awaited()


# ── Direct trigger tests ──────────────────────────────────────────


class TestProcessTrigger:
    @pytest.mark.asyncio
    @patch("channels.outbound_consumer.cache_active_call")
    @patch("channels.outbound_consumer.update_outbound_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.create_outbound_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.create_vapi_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.get_or_authenticate", new_callable=AsyncMock)
    async def test_trigger_returns_call_info(
        self, mock_auth, mock_vapi, mock_create, mock_update, mock_cache
    ):
        mock_auth.return_value = {
            "bearer_token": "jwt", "client_id": "c1", "customer_id": "cu1",
            "user_id": "u1", "user_name": "Jane", "client_name": "TestCo",
            "timezone": "US/Eastern", "support_number": "",
        }
        mock_create.return_value = "call-trigger-1"
        mock_vapi.return_value = {"id": "vapi-trigger-abc"}

        result = await process_trigger({
            "project_id": "proj-1",
            "client_id": "client-1",
            "customer_phone": "+15551234567",
        })

        assert result["call_id"] == "call-trigger-1"
        assert result["vapi_call_id"] == "vapi-trigger-abc"
        assert result["status"] == "calling"
