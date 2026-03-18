"""Tests for SMS channel — webhook, background processing, Pinpoint sending."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture()
def client():
    return TestClient(app)


class TestSmsWebhookSubscription:
    """SNS SubscriptionConfirmation handling."""

    def test_subscription_confirmation(self, client):
        """Auto-confirm SNS subscription by visiting SubscribeURL."""
        with patch("channels.sms.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_ctx

            resp = client.post("/sms/webhook", json={
                "Type": "SubscriptionConfirmation",
                "SubscribeURL": "https://sns.us-east-1.amazonaws.com/confirm?token=abc123",
            })

            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"
            mock_client.get.assert_awaited_once()

    def test_subscription_confirmation_no_url(self, client):
        """SubscriptionConfirmation without SubscribeURL is a no-op."""
        resp = client.post("/sms/webhook", json={
            "Type": "SubscriptionConfirmation",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestSmsWebhookNotification:
    """SNS Notification (inbound SMS) handling."""

    def test_valid_sms_triggers_background_task(self, client):
        """Valid SMS notification queues a background task."""
        sms_data = {
            "originationNumber": "+14702832382",
            "messageBody": "Show me my projects",
            "destinationNumber": "+15551234567",
        }
        with patch("channels.sms._process_and_reply", new_callable=AsyncMock) as mock_process:
            resp = client.post("/sms/webhook", json={
                "Type": "Notification",
                "Message": json.dumps(sms_data),
            })

            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"
            # Background task is queued (FastAPI runs it after response)

    def test_missing_origination_number(self, client):
        """SMS without originationNumber returns ok but skips processing."""
        sms_data = {
            "messageBody": "Show me my projects",
            "destinationNumber": "+15551234567",
        }
        resp = client.post("/sms/webhook", json={
            "Type": "Notification",
            "Message": json.dumps(sms_data),
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_missing_message_body(self, client):
        """SMS without messageBody returns ok but skips processing."""
        sms_data = {
            "originationNumber": "+14702832382",
            "destinationNumber": "+15551234567",
        }
        resp = client.post("/sms/webhook", json={
            "Type": "Notification",
            "Message": json.dumps(sms_data),
        })
        assert resp.status_code == 200

    def test_invalid_json_message(self, client):
        """Non-JSON SNS Message is handled gracefully."""
        resp = client.post("/sms/webhook", json={
            "Type": "Notification",
            "Message": "not valid json {{{",
        })
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_unrecognized_sns_type(self, client):
        """Unknown SNS Type returns ok."""
        resp = client.post("/sms/webhook", json={
            "Type": "UnsubscribeConfirmation",
        })
        assert resp.status_code == 200


class TestProcessAndReply:
    """Background task: authenticate → orchestrate → format → send."""

    @patch("channels.sms._send_sms", new_callable=AsyncMock)
    @patch("channels.sms.get_orchestrator")
    @patch("channels.sms.get_or_authenticate", new_callable=AsyncMock)
    async def test_full_flow(self, mock_auth, mock_get_orch, mock_send):
        """Happy path: auth → orchestrate → format → send SMS."""
        from channels.sms import _process_and_reply

        mock_auth.return_value = {
            "bearer_token": "jwt-123",
            "client_id": "c1",
            "customer_id": "cust1",
            "user_id": "u1",
            "user_name": "John",
        }

        mock_response = MagicMock()
        mock_response.output = "Your **appointment** is on March 15."
        mock_orch = AsyncMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        mock_get_orch.return_value = mock_orch

        await _process_and_reply(
            from_phone="+14702832382",
            message_body="When is my appointment?",
            dest_phone="+15551234567",
        )

        mock_auth.assert_awaited_once()
        mock_orch.route_request.assert_awaited_once()

        # Verify SMS was sent with formatted text (no markdown)
        mock_send.assert_awaited_once()
        sent_text = mock_send.call_args[0][1]
        assert "**" not in sent_text  # Markdown stripped by format_for_sms
        assert "appointment" in sent_text.lower()

    @patch("channels.sms._send_sms", new_callable=AsyncMock)
    @patch("channels.sms.get_or_authenticate", new_callable=AsyncMock)
    async def test_auth_failure_sends_error_sms(self, mock_auth, mock_send):
        """Auth failure sends an error SMS to the user."""
        from channels.sms import _process_and_reply

        mock_auth.side_effect = RuntimeError("Auth API down")

        await _process_and_reply(
            from_phone="+14702832382",
            message_body="Hello",
            dest_phone="+15551234567",
        )

        mock_send.assert_awaited_once()
        sent_text = mock_send.call_args[0][1]
        assert "couldn't verify" in sent_text.lower()

    @patch("channels.sms._send_sms", new_callable=AsyncMock)
    @patch("channels.sms.get_orchestrator")
    @patch("channels.sms.get_or_authenticate", new_callable=AsyncMock)
    async def test_orchestrator_failure_sends_error_sms(self, mock_auth, mock_get_orch, mock_send):
        """Orchestrator error sends an error SMS to the user."""
        from channels.sms import _process_and_reply

        mock_auth.return_value = {
            "bearer_token": "jwt-123",
            "client_id": "c1",
            "customer_id": "cust1",
            "user_id": "u1",
            "user_name": "John",
        }
        mock_orch = AsyncMock()
        mock_orch.route_request = AsyncMock(side_effect=RuntimeError("Bedrock timeout"))
        mock_get_orch.return_value = mock_orch

        await _process_and_reply(
            from_phone="+14702832382",
            message_body="Show projects",
            dest_phone="+15551234567",
        )

        mock_send.assert_awaited_once()
        sent_text = mock_send.call_args[0][1]
        assert "trouble" in sent_text.lower()

    @patch("channels.sms._send_sms", new_callable=AsyncMock)
    @patch("channels.sms.get_orchestrator")
    @patch("channels.sms.get_or_authenticate", new_callable=AsyncMock)
    async def test_session_id_derived_from_phone(self, mock_auth, mock_get_orch, mock_send):
        """Session ID is derived from normalized phone number."""
        from channels.sms import _process_and_reply

        mock_auth.return_value = {
            "bearer_token": "jwt",
            "client_id": "c",
            "customer_id": "cu",
            "user_id": "u",
            "user_name": "Test",
        }
        mock_response = MagicMock()
        mock_response.output = "Hello"
        mock_orch = AsyncMock()
        mock_orch.route_request = AsyncMock(return_value=mock_response)
        mock_get_orch.return_value = mock_orch

        await _process_and_reply(
            from_phone="+14702832382",
            message_body="Hi",
            dest_phone="+15551234567",
        )

        # Check session_id in route_request call
        call_kwargs = mock_orch.route_request.call_args
        assert call_kwargs.kwargs["session_id"] == "sms:4702832382"
        assert call_kwargs.kwargs["additional_params"]["channel"] == "sms"


class TestSendSms:
    """SMS sender via pinpoint-sms-voice-v2."""

    @patch("channels.sms.boto3")
    async def test_send_sms_calls_pinpoint_v2(self, mock_boto3):
        """_send_sms calls pinpoint-sms-voice-v2 send_text_message."""
        from channels.sms import _send_sms

        mock_sms_client = MagicMock()
        mock_boto3.client.return_value = mock_sms_client

        await _send_sms("+14702832382", "Your appointment is confirmed.")

        mock_boto3.client.assert_called_once_with("pinpoint-sms-voice-v2", region_name="us-east-1")
        mock_sms_client.send_text_message.assert_called_once()
        call_kwargs = mock_sms_client.send_text_message.call_args.kwargs
        assert call_kwargs["DestinationPhoneNumber"] == "+14702832382"
        assert call_kwargs["MessageBody"] == "Your appointment is confirmed."
        assert call_kwargs["MessageType"] == "TRANSACTIONAL"
        assert call_kwargs["OriginationIdentity"] == "+15551234567"
        assert call_kwargs["ConfigurationSetName"] == "scheduling-agent-sms-config-test"

    @patch("channels.sms.boto3")
    async def test_send_sms_handles_error(self, mock_boto3):
        """_send_sms handles API errors without raising."""
        from channels.sms import _send_sms

        mock_sms_client = MagicMock()
        mock_sms_client.send_text_message.side_effect = RuntimeError("SMS service error")
        mock_boto3.client.return_value = mock_sms_client

        # Should not raise
        await _send_sms("+14702832382", "Test message")
