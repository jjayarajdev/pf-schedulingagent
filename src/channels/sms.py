"""SMS channel — POST /sms/webhook (inbound SMS via AWS SNS + Pinpoint).

AWS End User Messaging (pinpoint-sms-voice-v2) forwards inbound SMS messages
to an SNS topic.  SNS delivers them as HTTP POST notifications to this webhook.
The handler authenticates the sender via phone_auth, processes the message
through the AgentSquad orchestrator, formats the reply for SMS, and sends it
back via ``send_text_message``.

SNS also sends a ``SubscriptionConfirmation`` request when the topic is
first subscribed — this endpoint auto-confirms by GETting the SubscribeURL.
"""

import json
import logging
import time

import boto3
import httpx

from auth.context import AuthContext
from auth.phone_auth import get_or_authenticate, normalize_phone
from channels.conversation_log import log_conversation
from channels.formatters import format_for_sms
from config import get_settings
from observability.logging import RequestContext
from orchestrator import get_orchestrator
from orchestrator.response_utils import extract_response_text

logger = logging.getLogger(__name__)

from fastapi import APIRouter, BackgroundTasks, Request

router = APIRouter(prefix="/sms", tags=["sms"])


@router.post(
    "/webhook",
    summary="Inbound SMS webhook (via SNS)",
    description=(
        "Receives inbound SMS messages from AWS Pinpoint via SNS notifications.\n\n"
        "- **SubscriptionConfirmation**: auto-confirmed by visiting the SubscribeURL.\n"
        "- **Notification**: parsed, authenticated, processed through the orchestrator, "
        "and an SMS reply is sent back via Pinpoint."
    ),
)
async def sms_webhook(request: Request, background_tasks: BackgroundTasks):
    """Handle inbound SMS from SNS.

    Returns 200 OK immediately; message processing runs in a background task
    so SNS does not time out waiting for a response.
    """
    body = await request.json()

    sns_type = body.get("Type", "")

    # ── SNS subscription confirmation ────────────────────────────────────
    if sns_type == "SubscriptionConfirmation":
        subscribe_url = body.get("SubscribeURL", "")
        if subscribe_url:
            logger.info("Confirming SNS subscription: %s", subscribe_url[:120])
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    await client.get(subscribe_url)
                logger.info("SNS subscription confirmed")
            except Exception:
                logger.exception("Failed to confirm SNS subscription")
        return {"status": "ok"}

    # ── SNS notification (inbound SMS) ───────────────────────────────────
    if sns_type == "Notification":
        raw_message = body.get("Message", "")
        try:
            sms_data = json.loads(raw_message) if isinstance(raw_message, str) else raw_message
        except (json.JSONDecodeError, TypeError):
            logger.error("Failed to parse SNS Message as JSON: %s", raw_message[:200])
            return {"status": "ok"}

        from_phone = sms_data.get("originationNumber", "")
        message_body = sms_data.get("messageBody", "")
        dest_phone = sms_data.get("destinationNumber", "")

        if not from_phone or not message_body:
            logger.warning("SMS missing originationNumber or messageBody")
            return {"status": "ok"}

        logger.info(
            "Inbound SMS from ***%s: %s",
            from_phone[-4:] if len(from_phone) >= 4 else from_phone,
            message_body[:80],
        )

        # Process in background so we return 200 to SNS immediately
        background_tasks.add_task(
            _process_and_reply,
            from_phone=from_phone,
            message_body=message_body,
            dest_phone=dest_phone,
        )
        return {"status": "ok"}

    # Unrecognized SNS type — acknowledge
    logger.warning("Unrecognized SNS Type: %s", sns_type)
    return {"status": "ok"}


# ── Background processing ────────────────────────────────────────────────


async def _process_and_reply(
    from_phone: str,
    message_body: str,
    dest_phone: str,
) -> None:
    """Authenticate, process the message through the orchestrator, and reply via SMS."""
    phone_normalized = normalize_phone(from_phone)
    session_id = f"sms:{phone_normalized}"
    user_id = phone_normalized or "sms-anonymous"

    RequestContext.set(session_id=session_id, user_id=user_id, channel="sms")

    # Step 1: Authenticate the caller
    try:
        creds = await get_or_authenticate(from_phone, dest_phone)
        AuthContext.set(
            auth_token=creds.get("bearer_token", ""),
            client_id=creds.get("client_id", ""),
            customer_id=creds.get("customer_id", creds.get("user_id", "")),
            user_id=creds.get("user_id", user_id),
            user_name=creds.get("user_name", ""),
        )
        user_id = creds.get("user_id", user_id)
    except Exception:
        logger.exception("Phone auth failed for SMS from ***%s", phone_normalized[-4:])
        await _send_sms(
            from_phone,
            "Sorry, I couldn't verify your account. Please try again or contact support.",
        )
        return

    # Step 2: Process through the orchestrator
    agent_name = ""
    start_time = time.monotonic()
    try:
        orchestrator = get_orchestrator()
        response = await orchestrator.route_request(
            user_input=message_body,
            user_id=user_id,
            session_id=session_id,
            additional_params={"channel": "sms"},
        )
        response_text = extract_response_text(response.output)
        agent_name = response.metadata.agent_name if response.metadata else ""
    except Exception:
        logger.exception("Orchestrator error for SMS session %s", session_id)
        await _send_sms(
            from_phone,
            "Sorry, I had trouble processing your request. Please try again.",
        )
        return
    elapsed_ms = int((time.monotonic() - start_time) * 1000)

    # Step 3: Format for SMS and send
    sms_text = format_for_sms(response_text)
    logger.info(
        "SMS reply to ***%s: %d chars (raw %d) elapsed_ms=%d",
        phone_normalized[-4:],
        len(sms_text),
        len(response_text),
        elapsed_ms,
    )
    await _send_sms(from_phone, sms_text)

    # Fire-and-forget conversation log
    await log_conversation(
        session_id=session_id,
        user_id=user_id,
        user_message=message_body,
        bot_response=sms_text,
        agent_name=agent_name,
        channel="sms",
        response_time_ms=elapsed_ms,
    )


# ── SMS sender (pinpoint-sms-voice-v2) ─────────────────────────────────


async def _send_sms(phone: str, message: str) -> None:
    """Send an SMS reply via AWS End User Messaging ``send_text_message`` API.

    Uses ``pinpoint-sms-voice-v2`` (the same service v1.2.9 uses) instead of
    the legacy ``pinpoint`` ``send_messages`` API.

    Args:
        phone: Destination phone number in E.164 format (e.g. ``+14702832382``).
        message: Plain-text message body (must already be SMS-safe).
    """
    settings = get_settings()
    try:
        sms_client = boto3.client("pinpoint-sms-voice-v2", region_name=settings.aws_region)
        params: dict = {
            "DestinationPhoneNumber": phone,
            "OriginationIdentity": settings.sms_origination_number,
            "MessageBody": message,
            "MessageType": "TRANSACTIONAL",
        }
        if settings.sms_configuration_set:
            params["ConfigurationSetName"] = settings.sms_configuration_set
        sms_client.send_text_message(**params)
        logger.info("SMS sent to ***%s (%d chars)", phone[-4:] if len(phone) >= 4 else phone, len(message))
    except Exception:
        logger.exception("Failed to send SMS to %s", phone[-4:] if len(phone) >= 4 else phone)
