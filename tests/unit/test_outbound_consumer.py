"""Tests for outbound_consumer.py — SQS consumer and message processing."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from channels.outbound_consumer import (
    _prefetch_project_data,
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
    @patch("channels.outbound_consumer._prefetch_project_data", new_callable=AsyncMock, return_value={})
    @patch("channels.outbound_consumer.get_settings")
    @patch("channels.outbound_consumer.cache_active_call")
    @patch("channels.outbound_consumer.update_outbound_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.create_outbound_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.create_vapi_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.get_or_authenticate", new_callable=AsyncMock)
    async def test_happy_path(
        self, mock_auth, mock_vapi, mock_create, mock_update, mock_cache,
        mock_settings, mock_prefetch, mock_sqs
    ):
        mock_settings.return_value.vapi_phone_number = "+14588990940"
        mock_auth.return_value = {
            "bearer_token": "jwt", "client_id": "c1", "customer_id": "cu1",
            "user_id": "u1", "user_name": "Jane", "client_name": "TestCo",
            "timezone": "US/Eastern", "support_number": "+15559999999",
        }
        mock_create.return_value = "call-001"
        mock_vapi.return_value = {"id": "vapi-abc"}

        msg = _make_sqs_message({
            "event": "auto_call_ready",
            "project_id": "proj-1",
            "client_id": "client-1",
            "customer": {
                "customer_id": "cu1",
                "first_name": "Jane",
                "last_name": "Doe",
                "primary_phone": "+15551234567",
            },
            "tenant_info": {
                "category": "Flooring",
                "type": "Installation",
            },
        })

        await _process_outbound_message(msg, mock_sqs, "https://queue-url")

        mock_auth.assert_awaited_once_with("+15551234567", "+14588990940")
        mock_prefetch.assert_awaited_once()
        mock_create.assert_awaited_once()
        # Verify prefetched data is stored in call record
        call_data = mock_create.call_args[0][0]
        assert "prefetched" in call_data
        mock_vapi.assert_awaited_once()
        mock_update.assert_awaited_once()
        mock_cache.assert_called_once()
        mock_sqs.delete_message.assert_called_once()

    @pytest.mark.asyncio
    @patch("channels.outbound_consumer._prefetch_project_data", new_callable=AsyncMock, return_value={})
    @patch("channels.outbound_consumer.get_settings")
    @patch("channels.outbound_consumer.cache_active_call")
    @patch("channels.outbound_consumer.update_outbound_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.create_outbound_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.create_vapi_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.get_or_authenticate", new_callable=AsyncMock)
    async def test_pf_nested_payload_format(
        self, mock_auth, mock_vapi, mock_create, mock_update, mock_cache,
        mock_settings, mock_prefetch, mock_sqs
    ):
        """PF backend sends nested customer/tenant_info — verify we extract correctly."""
        mock_settings.return_value.vapi_phone_number = "+14588990940"
        mock_auth.return_value = {
            "bearer_token": "jwt", "client_id": "c1", "customer_id": "cu1",
            "user_id": "u1", "user_name": "John Smith", "client_name": "TestCo",
            "timezone": "US/Eastern", "support_number": "+15559999999",
        }
        mock_create.return_value = "call-pf"
        mock_vapi.return_value = {"id": "vapi-pf"}

        msg = _make_sqs_message({
            "event": "auto_call_ready",
            "client_id": "10003",
            "project_id": "90000120",
            "customer_id": "90000033",
            "store_id": "store-1",
            "tenant_info": {
                "client_id": "10003",
                "source": "pf360",
                "category": "Balcony grill",
                "type": "Installation",
            },
            "customer": {
                "customer_id": "90000033",
                "first_name": "John",
                "last_name": "Smith",
                "primary_phone": "+18585551234",
            },
            "project": {
                "project_number": "6789",
                "status_id": "42",
                "status_name": "Ready for Auto Call",
            },
        })

        await _process_outbound_message(msg, mock_sqs, "https://queue-url")

        # Verify phone extracted from nested customer object
        mock_auth.assert_awaited_once_with("+18585551234", "+14588990940")
        # Verify call record has correct extracted fields
        call_data = mock_create.call_args[0][0]
        assert call_data["customer_name"] == "John Smith"
        assert call_data["project_type"] == "Balcony grill Installation"
        assert call_data["phone_primary"] == "+18585551234"
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
    @patch("channels.outbound_consumer.get_settings")
    @patch("channels.outbound_consumer.create_outbound_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.get_or_authenticate", new_callable=AsyncMock)
    async def test_auth_failure_creates_failed_record(
        self, mock_auth, mock_create, mock_settings, mock_sqs
    ):
        from auth.phone_auth import AuthenticationError

        mock_settings.return_value.vapi_phone_number = "+14588990940"
        mock_auth.side_effect = AuthenticationError("auth failed")
        mock_create.return_value = "call-fail"

        msg = _make_sqs_message({
            "project_id": "proj-1",
            "client_id": "c1",
            "customer": {
                "primary_phone": "+15551234567",
            },
        })

        await _process_outbound_message(msg, mock_sqs, "https://queue-url")

        mock_create.assert_awaited_once()
        create_data = mock_create.call_args[0][0]
        assert create_data["status"] == "failed"
        mock_sqs.delete_message.assert_called_once()


# ── Retry tests ───────────────────────────────────────────────────


class TestRetryOutboundCall:
    @pytest.mark.asyncio
    @patch("channels.outbound_consumer.get_settings")
    @patch("channels.outbound_consumer.cache_active_call")
    @patch("channels.outbound_consumer.update_outbound_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.create_vapi_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.get_or_authenticate", new_callable=AsyncMock)
    async def test_retries_on_alternate_number(
        self, mock_auth, mock_vapi, mock_update, mock_cache, mock_settings
    ):
        mock_settings.return_value.vapi_phone_number = "+14588990940"
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

        mock_auth.assert_awaited_once_with("+15559876543", "+14588990940")
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
    @patch("channels.outbound_consumer._prefetch_project_data", new_callable=AsyncMock, return_value={})
    @patch("channels.outbound_consumer.get_settings")
    @patch("channels.outbound_consumer.cache_active_call")
    @patch("channels.outbound_consumer.update_outbound_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.create_outbound_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.create_vapi_call", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.get_or_authenticate", new_callable=AsyncMock)
    async def test_trigger_returns_call_info(
        self, mock_auth, mock_vapi, mock_create, mock_update, mock_cache,
        mock_settings, mock_prefetch
    ):
        mock_settings.return_value.vapi_phone_number = "+14588990940"
        mock_auth.return_value = {
            "bearer_token": "jwt", "client_id": "c1", "customer_id": "cu1",
            "user_id": "u1", "user_name": "Jane", "client_name": "TestCo",
            "timezone": "US/Eastern", "support_number": "",
        }
        mock_create.return_value = "call-trigger-1"
        mock_vapi.return_value = {"id": "vapi-trigger-abc"}

        result = await process_trigger({
            "event": "auto_call_ready",
            "project_id": "proj-1",
            "client_id": "client-1",
            "customer": {
                "customer_id": "cu1",
                "first_name": "Jane",
                "last_name": "Doe",
                "primary_phone": "+15551234567",
            },
        })

        assert result["call_id"] == "call-trigger-1"
        assert result["vapi_call_id"] == "vapi-trigger-abc"
        assert result["status"] == "calling"
        mock_prefetch.assert_awaited_once()


# ── Pre-fetch tests ──────────────────────────────────────────────


class TestPrefetchProjectData:
    @pytest.mark.asyncio
    @patch("channels.outbound_consumer.get_installation_address", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.get_available_dates", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.get_project_details", new_callable=AsyncMock)
    async def test_prefetch_returns_dates_and_address(
        self, mock_details, mock_dates, mock_addr
    ):
        mock_details.return_value = json.dumps({
            "project": {"id": "123", "projectNumber": "P-001", "category": "Flooring"},
        })
        mock_dates.return_value = json.dumps({
            "available_dates": ["2026-04-14", "2026-04-15"],
            "available_time_slots": ["9:00 AM", "1:00 PM"],
            "request_id": 42,
        })
        mock_addr.return_value = json.dumps({
            "address": {
                "address1": "123 Main St",
                "city": "Austin",
                "state": "TX",
                "zipcode": "78701",
            },
        })

        creds = {
            "bearer_token": "jwt", "client_id": "c1",
            "customer_id": "cu1", "user_id": "u1",
            "user_name": "Jane", "timezone": "US/Eastern",
        }

        result = await _prefetch_project_data(
            creds=creds, project_id="123", customer_id="cu1", client_id="c1",
        )

        assert result["dates"]["available_dates"] == ["2026-04-14", "2026-04-15"]
        assert result["dates"]["request_id"] == 42
        assert result["address"]["city"] == "Austin"
        assert result["project"]["projectNumber"] == "P-001"
        mock_details.assert_awaited_once_with("123")
        mock_dates.assert_awaited_once_with("123")
        mock_addr.assert_awaited_once_with("123")

    @pytest.mark.asyncio
    @patch("channels.outbound_consumer.get_installation_address", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.get_available_dates", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.get_project_details", new_callable=AsyncMock)
    async def test_prefetch_handles_failures_gracefully(
        self, mock_details, mock_dates, mock_addr
    ):
        mock_details.side_effect = Exception("API down")
        mock_dates.return_value = "not json"
        mock_addr.return_value = "not json"

        result = await _prefetch_project_data(
            creds={"bearer_token": "jwt", "client_id": "c1"},
            project_id="123", customer_id="cu1", client_id="c1",
        )

        # Should not raise; returns whatever it could gather
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    @patch("channels.outbound_consumer.get_installation_address", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.get_available_dates", new_callable=AsyncMock)
    @patch("channels.outbound_consumer.get_project_details", new_callable=AsyncMock)
    async def test_prefetch_with_weather_dates(
        self, mock_details, mock_dates, mock_addr
    ):
        mock_details.return_value = json.dumps({
            "project": {"id": "123", "projectNumber": "P-001"},
        })
        mock_dates.return_value = json.dumps({
            "available_dates": ["2026-04-14"],
            "dates_with_weather": [
                ["2026-04-14", "Tue", "Sunny", 78, "[GOOD]"],
            ],
            "request_id": 99,
        })
        mock_addr.return_value = json.dumps({"address": {"address1": "456 Oak"}})

        result = await _prefetch_project_data(
            creds={"bearer_token": "jwt", "client_id": "c1"},
            project_id="123", customer_id="cu1", client_id="c1",
        )

        assert result["dates"]["dates_with_weather"][0][2] == "Sunny"
