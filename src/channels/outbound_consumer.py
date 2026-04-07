"""SQS consumer for outbound scheduling calls.

Background asyncio task that long-polls the SQS queue and processes
outbound call requests from PF's rule engine.

Flow per message:
1. Parse SQS message body (project_id, client_id, customer_phone, etc.)
2. Authenticate customer via phone-call-login → get bearer_token, customer_id
3. Create outbound call record in DynamoDB (status=pending)
4. Call Vapi POST /call with serverUrl → our webhook
5. Update record with vapi_call_id, status=calling
6. Cache in _active_calls (for fast lookup during tool-call events)
7. Delete SQS message on success
"""

import asyncio
import json
import logging

import boto3

from auth.context import AuthContext
from auth.phone_auth import AuthenticationError, get_or_authenticate
from channels.outbound_store import (
    cache_active_call,
    create_outbound_call,
    get_active_call,
    get_outbound_call,
    remove_active_call,
    update_outbound_call,
)
from channels.outbound_vapi import create_vapi_call
from config import get_settings

logger = logging.getLogger(__name__)

# Consumer state
_consumer_task: asyncio.Task | None = None
_shutdown_event = asyncio.Event()

# Vapi webhook URL per environment
_WEBHOOK_URLS = {
    "dev": "https://schedulingagent.dev.projectsforce.com/vapi/webhook",
    "qa": "https://schedulingagent.qa.projectsforce.com/vapi/webhook",
    "staging": "https://schedulingagent.staging.projectsforce.com/vapi/webhook",
    "prod": "https://schedulingagent.apps.projectsforce.com/vapi/webhook",
}


def _get_webhook_url() -> str:
    env = get_settings().environment
    return _WEBHOOK_URLS.get(env, _WEBHOOK_URLS["dev"])


# ── Public lifecycle API ──────────────────────────────────────────────


async def start_outbound_consumer() -> None:
    """Start the SQS consumer loop. Called from main.py on startup."""
    global _consumer_task
    settings = get_settings()

    if not settings.outbound_queue_url:
        logger.info("Outbound queue URL not configured — consumer disabled")
        return

    _shutdown_event.clear()
    _consumer_task = asyncio.create_task(_consumer_loop())
    logger.info("Outbound SQS consumer started (queue=%s)", settings.outbound_queue_url)


async def stop_outbound_consumer() -> None:
    """Graceful shutdown — signal consumer to stop and wait for it."""
    global _consumer_task
    if _consumer_task is None:
        return

    _shutdown_event.set()
    try:
        await asyncio.wait_for(_consumer_task, timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("Outbound consumer shutdown timed out — cancelling")
        _consumer_task.cancel()
    _consumer_task = None
    logger.info("Outbound SQS consumer stopped")


# ── Consumer loop ────────────────────────────────────────────────────


async def _consumer_loop() -> None:
    """Long-poll SQS and process messages until shutdown."""
    settings = get_settings()
    sqs = boto3.client("sqs", region_name=settings.aws_region)
    queue_url = settings.outbound_queue_url

    while not _shutdown_event.is_set():
        try:
            # Long-poll (blocking in thread pool to avoid blocking the event loop)
            response = await asyncio.to_thread(
                sqs.receive_message,
                QueueUrl=queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=20,
                VisibilityTimeout=120,
            )

            messages = response.get("Messages", [])
            if not messages:
                continue

            for msg in messages:
                try:
                    await _process_outbound_message(msg, sqs, queue_url)
                except Exception:
                    logger.exception(
                        "Failed to process outbound message: %s",
                        msg.get("MessageId", "?"),
                    )
                    # Don't delete — let visibility timeout expire for retry
        except Exception:
            if not _shutdown_event.is_set():
                logger.exception("SQS receive error — retrying in 5s")
                await asyncio.sleep(5)


# ── Message processing ───────────────────────────────────────────────


async def _process_outbound_message(
    sqs_message: dict, sqs_client, queue_url: str
) -> None:
    """Process a single SQS message — authenticate, create record, initiate call."""
    message_id = sqs_message.get("MessageId", "unknown")
    receipt_handle = sqs_message.get("ReceiptHandle", "")

    try:
        body = json.loads(sqs_message.get("Body", "{}"))
    except json.JSONDecodeError:
        logger.error("Invalid JSON in SQS message %s — deleting", message_id)
        sqs_client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
        return

    customer_phone = body.get("customer_phone", "")
    customer_phone_alt = body.get("customer_phone_alt", "")
    project_id = body.get("project_id", "")
    client_id = body.get("client_id", "")
    customer_name = body.get("customer_name", "")
    customer_id = body.get("customer_id", "")
    project_type = body.get("project_type", "")
    vapi_phone_number_id = body.get("vapi_phone_number_id", "")

    if not customer_phone or not project_id:
        logger.error(
            "Missing required fields (phone=%s project=%s) in SQS message %s — deleting",
            customer_phone, project_id, message_id,
        )
        sqs_client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
        return

    logger.info(
        "Processing outbound call: project=%s phone=***%s client=%s",
        project_id, customer_phone[-4:], client_id,
    )

    # Step 1: Authenticate customer via phone-call-login
    try:
        creds = await get_or_authenticate(customer_phone)
    except AuthenticationError:
        logger.exception(
            "Auth failed for outbound call: project=%s phone=***%s",
            project_id, customer_phone[-4:],
        )
        # Create a failed record and delete message
        await create_outbound_call({
            "project_id": project_id,
            "client_id": client_id,
            "customer_phone": customer_phone,
            "phone_primary": customer_phone,
            "phone_alternate": customer_phone_alt,
            "customer_name": customer_name,
            "customer_id": customer_id,
            "project_type": project_type,
            "status": "failed",
            "call_result": {"error": "authentication_failed"},
            "sqs_message_id": message_id,
        })
        sqs_client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
        return

    # Populate names from creds if not in SQS message
    if not customer_name:
        customer_name = creds.get("user_name", "")
    if not customer_id:
        customer_id = creds.get("customer_id", creds.get("user_id", ""))
    client_name = creds.get("client_name", "ProjectsForce")

    # Step 2: Create outbound call record (status=pending)
    call_data = {
        "project_id": project_id,
        "client_id": client_id or creds.get("client_id", ""),
        "customer_id": customer_id,
        "customer_name": customer_name,
        "client_name": client_name,
        "call_type": "scheduling",
        "phone_primary": customer_phone,
        "phone_alternate": customer_phone_alt,
        "phone_used": customer_phone,
        "project_type": project_type,
        "vapi_phone_number_id": vapi_phone_number_id,
        "auth_creds": {
            "bearer_token": creds.get("bearer_token", ""),
            "client_id": creds.get("client_id", ""),
            "customer_id": creds.get("customer_id", creds.get("user_id", "")),
            "user_id": creds.get("user_id", ""),
            "user_name": creds.get("user_name", ""),
            "timezone": creds.get("timezone", "US/Eastern"),
            "support_number": creds.get("support_number", ""),
        },
        "sqs_message_id": message_id,
    }
    call_id = await create_outbound_call(call_data)
    call_data["call_id"] = call_id

    # Step 3: Initiate Vapi call
    try:
        webhook_url = _get_webhook_url()
        vapi_response = await create_vapi_call(
            phone_number_id=vapi_phone_number_id,
            customer_phone=customer_phone,
            customer_name=customer_name,
            server_url=webhook_url,
            metadata={"call_id": call_id, "project_id": project_id},
        )
        vapi_call_id = vapi_response.get("id", "")

        # Step 4: Update record with Vapi call ID, status=calling
        await update_outbound_call(call_id, {
            "vapi_call_id": vapi_call_id,
            "status": "calling",
        })
        call_data["vapi_call_id"] = vapi_call_id
        call_data["status"] = "calling"

        # Step 5: Cache for fast lookup during tool-call events
        if vapi_call_id:
            cache_active_call(vapi_call_id, call_data)

        logger.info(
            "Outbound call initiated: call_id=%s vapi_call_id=%s project=%s",
            call_id, vapi_call_id, project_id,
        )
    except Exception:
        logger.exception("Failed to initiate Vapi call: call_id=%s", call_id)
        await update_outbound_call(call_id, {
            "status": "failed",
            "call_result": {"error": "vapi_call_failed"},
        })
        # Still delete from SQS — retrying won't help if Vapi is down
        # The DLQ policy will handle real transient failures via visibility timeout

    # Step 6: Delete SQS message (call initiated successfully or permanently failed)
    sqs_client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
    logger.info("Deleted SQS message %s after processing", message_id)


# ── Retry logic ──────────────────────────────────────────────────────


async def retry_outbound_call(outbound_call: dict) -> None:
    """Retry an outbound call on the alternate phone number.

    Called from the end-of-call handler when the outcome is no_answer/voicemail
    and an alternate number is available.
    """
    call_id = outbound_call.get("call_id", "")
    attempt = outbound_call.get("attempt_number", 1)
    max_attempts = outbound_call.get("max_attempts", 2)
    alt_phone = outbound_call.get("phone_alternate", "")

    if attempt >= max_attempts or not alt_phone:
        logger.info(
            "No retry for call %s: attempt=%d/%d alt_phone=%s",
            call_id, attempt, max_attempts, bool(alt_phone),
        )
        return

    logger.info("Retrying outbound call %s on alternate number ***%s", call_id, alt_phone[-4:])

    next_attempt = attempt + 1

    # Re-authenticate with alternate phone
    try:
        creds = await get_or_authenticate(alt_phone)
    except AuthenticationError:
        logger.exception("Auth failed for retry: call_id=%s phone=***%s", call_id, alt_phone[-4:])
        await update_outbound_call(call_id, {
            "status": "failed",
            "call_result": {"error": "retry_auth_failed"},
            "attempt_number": next_attempt,
        })
        return

    # Update record with new attempt
    auth_creds = {
        "bearer_token": creds.get("bearer_token", ""),
        "client_id": creds.get("client_id", ""),
        "customer_id": creds.get("customer_id", creds.get("user_id", "")),
        "user_id": creds.get("user_id", ""),
        "user_name": creds.get("user_name", ""),
        "timezone": creds.get("timezone", "US/Eastern"),
        "support_number": creds.get("support_number", ""),
    }

    await update_outbound_call(call_id, {
        "status": "calling",
        "attempt_number": next_attempt,
        "phone_used": alt_phone,
        "auth_creds": auth_creds,
    })

    # Initiate call on alternate number
    try:
        webhook_url = _get_webhook_url()
        vapi_response = await create_vapi_call(
            phone_number_id=outbound_call.get("vapi_phone_number_id", ""),
            customer_phone=alt_phone,
            customer_name=outbound_call.get("customer_name", ""),
            server_url=webhook_url,
            metadata={
                "call_id": call_id,
                "project_id": outbound_call.get("project_id", ""),
            },
        )
        vapi_call_id = vapi_response.get("id", "")
        await update_outbound_call(call_id, {"vapi_call_id": vapi_call_id})

        # Update cache
        updated_call = {**outbound_call, **auth_creds, "auth_creds": auth_creds}
        updated_call["vapi_call_id"] = vapi_call_id
        updated_call["attempt_number"] = next_attempt
        updated_call["phone_used"] = alt_phone
        if vapi_call_id:
            cache_active_call(vapi_call_id, updated_call)

        logger.info("Retry call initiated: call_id=%s vapi_call_id=%s", call_id, vapi_call_id)
    except Exception:
        logger.exception("Retry Vapi call failed: call_id=%s", call_id)
        await update_outbound_call(call_id, {
            "status": "failed",
            "call_result": {"error": "retry_vapi_call_failed"},
        })


# ── Direct trigger (for manual/dev use) ──────────────────────────────


async def process_trigger(request_data: dict) -> dict:
    """Process a manual outbound call trigger (bypasses SQS).

    Called by the /outbound/trigger endpoint. Uses the same flow as
    SQS processing but without the SQS message envelope.

    Returns dict with call_id, vapi_call_id, status.
    """
    customer_phone = request_data.get("customer_phone", "")
    project_id = request_data.get("project_id", "")

    # Authenticate
    creds = await get_or_authenticate(customer_phone)

    customer_name = request_data.get("customer_name", "") or creds.get("user_name", "")
    customer_id = request_data.get("customer_id", "") or creds.get("customer_id", "")
    client_name = creds.get("client_name", "ProjectsForce")

    call_data = {
        "project_id": project_id,
        "client_id": request_data.get("client_id", "") or creds.get("client_id", ""),
        "customer_id": customer_id,
        "customer_name": customer_name,
        "client_name": client_name,
        "call_type": "scheduling",
        "phone_primary": customer_phone,
        "phone_alternate": request_data.get("customer_phone_alt", ""),
        "phone_used": customer_phone,
        "project_type": request_data.get("project_type", ""),
        "vapi_phone_number_id": request_data.get("vapi_phone_number_id", ""),
        "auth_creds": {
            "bearer_token": creds.get("bearer_token", ""),
            "client_id": creds.get("client_id", ""),
            "customer_id": creds.get("customer_id", creds.get("user_id", "")),
            "user_id": creds.get("user_id", ""),
            "user_name": creds.get("user_name", ""),
            "timezone": creds.get("timezone", "US/Eastern"),
            "support_number": creds.get("support_number", ""),
        },
    }
    call_id = await create_outbound_call(call_data)
    call_data["call_id"] = call_id

    # Initiate Vapi call
    webhook_url = _get_webhook_url()
    vapi_response = await create_vapi_call(
        phone_number_id=request_data.get("vapi_phone_number_id", ""),
        customer_phone=customer_phone,
        customer_name=customer_name,
        server_url=webhook_url,
        metadata={"call_id": call_id, "project_id": project_id},
    )
    vapi_call_id = vapi_response.get("id", "")

    await update_outbound_call(call_id, {
        "vapi_call_id": vapi_call_id,
        "status": "calling",
    })
    call_data["vapi_call_id"] = vapi_call_id
    if vapi_call_id:
        cache_active_call(vapi_call_id, call_data)

    return {
        "call_id": call_id,
        "vapi_call_id": vapi_call_id,
        "status": "calling",
    }
