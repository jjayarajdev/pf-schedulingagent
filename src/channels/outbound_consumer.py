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
import re

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
from tools.project_rules import ProjectStatusRules
from tools.scheduling import (
    get_available_dates,
    get_installation_address,
    get_project_details,
)

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


def _normalize_e164(phone: str) -> str:
    """Normalize a phone number to E.164 format (+1XXXXXXXXXX)."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if digits:
        return f"+{digits}"
    return ""


def _extract_pf_payload(body: dict) -> dict:
    """Extract fields from PF backend nested payload format.

    PF sends: customer.primary_phone, customer.first_name/last_name,
    tenant_info.category/type, tenant_info.tenant_vapi_phone_number.
    Used by both SQS consumer and trigger endpoint.
    """
    customer_obj = body.get("customer", {})
    tenant_info = body.get("tenant_info", {})

    first = customer_obj.get("first_name", "")
    last = customer_obj.get("last_name", "")

    category = tenant_info.get("category", "")
    ptype = tenant_info.get("type", "")
    if category and ptype:
        project_type = f"{category} {ptype}"
    else:
        project_type = category or ptype or ""

    # Vapi phone number ID: from payload, or fall back to config default
    vapi_phone_id = body.get("vapi_phone_number_id", "")
    if not vapi_phone_id:
        vapi_phone_id = get_settings().outbound_vapi_phone_id

    # customer.primary_phone = to_phone (who to call)
    customer_phone = _normalize_e164(customer_obj.get("primary_phone", ""))

    # tenant_vapi_phone_number = from_phone (caller ID / Vapi number to call from)
    # PF sends this as an array — take the first element.
    # PF may send ["undefined"] or "undefined" when not configured.
    _EMPTY_SENTINELS = {"undefined", "null", "none", ""}
    tenant_from = tenant_info.get("tenant_vapi_phone_number", "")
    if isinstance(tenant_from, list):
        tenant_from = tenant_from[0] if tenant_from else ""
    if str(tenant_from).lower().strip() in _EMPTY_SENTINELS:
        tenant_from = ""
    from_phone = _normalize_e164(tenant_from)

    return {
        "customer_phone": customer_phone,
        "customer_phone_alt": customer_obj.get("alternate_phone", ""),
        "from_phone": from_phone,
        "project_id": str(body.get("project_id", "")),
        "client_id": str(body.get("client_id", "")),
        "customer_id": str(customer_obj.get("customer_id", "") or body.get("customer_id", "")),
        "customer_name": f"{first} {last}".strip(),
        "project_type": project_type,
        "vapi_phone_number_id": vapi_phone_id,
    }


def _build_assistant_for_call(call_data: dict) -> tuple[dict, dict | None]:
    """Build the Vapi assistant config for an outbound call.

    Vapi's POST /call requires the full assistant config inline.
    We reuse the config builder from vapi.py, then strip ``server``
    and ``serverUrl`` (not allowed inside ``assistant`` in POST /call).
    Tool-call routing is handled by adding ``server`` to each tool.

    Returns:
        Tuple of (assistant_config, server_block).  The server_block
        must be placed at the top level of the POST /call payload so
        Vapi sends server events (end-of-call-report, status-update)
        to our webhook with the correct secret.
    """
    from channels.vapi import (
        _build_outbound_scheduling_config,
        _generate_outbound_greeting,
    )
    from config import get_secrets

    customer_name = call_data.get("customer_name", "")
    client_name = call_data.get("client_name", "ProjectsForce")
    support_number = (call_data.get("auth_creds") or {}).get("support_number", "")

    # Use actual project type from prefetched data (e.g., "Flooring Installation")
    # rather than SQS tenant_info metadata (which gives "project update")
    prefetched_project = (call_data.get("prefetched") or {}).get("project", {})
    project_type = (
        prefetched_project.get("projectType", "")
        or prefetched_project.get("category", "")
        or call_data.get("project_type", "")
    )

    greeting = _generate_outbound_greeting(
        customer_name=customer_name,
        client_name=client_name,
        project_type=project_type,
    )

    server_secret = get_secrets().vapi_api_key
    config = _build_outbound_scheduling_config(
        first_message=greeting,
        server_secret=server_secret,
        outbound_call=call_data,
        support_number=support_number,
        client_name=client_name,
    )

    # POST /call's assistant object accepts ``server`` (with url + secret)
    # for server events (end-of-call-report, status-update).  Strip only
    # ``serverUrl`` (the string-only shorthand) to avoid conflicts.
    server_block = config.get("server")
    config.pop("serverUrl", None)

    # voicemailDetection uses different field names in POST /call
    vm = config.get("voicemailDetection", {})
    if "voicemailDetectionTypes" in vm:
        # POST /call expects "enabled" boolean, not the detection types list
        config["voicemailDetection"] = {"provider": vm.get("provider", "vapi")}

    # Also set server on each tool so Vapi routes tool-calls to us
    if server_block:
        for tool in config.get("model", {}).get("tools", []):
            if tool.get("type") == "function":
                tool["server"] = server_block

    return config, server_block


async def _prefetch_project_data(
    creds: dict, project_id: str, customer_id: str, client_id: str,
) -> dict:
    """Pre-fetch project data at trigger time so the AI has it immediately.

    Sets AuthContext, loads projects, fetches available dates (with weather),
    and gets the installation address.  The result is injected into the
    outbound system prompt so the AI speaks dates/address without tool calls.

    Also primes ``_request_id_by_project`` so downstream ``get_time_slots``
    and ``confirm_appointment`` calls work without a second dates lookup.
    """
    AuthContext.set(
        auth_token=creds.get("bearer_token", ""),
        client_id=client_id,
        customer_id=customer_id,
        user_id=creds.get("user_id", ""),
        user_name=creds.get("user_name", ""),
        timezone=creds.get("timezone", "US/Eastern"),
        support_number=creds.get("support_number", ""),
    )

    prefetched: dict = {}

    try:
        # 1. Load project details (populates _projects_cache for address, weather, etc.)
        details_json = await get_project_details(project_id)
        try:
            details = json.loads(details_json)
            project = details.get("project", {})
            prefetched["project"] = project
            logger.info("Pre-fetch: project %s loaded", project.get("projectNumber", project_id))
        except (json.JSONDecodeError, TypeError):
            logger.warning("Pre-fetch: project details not JSON — %s", details_json[:200])

        # 2. Available dates + weather enrichment (also primes _request_id_by_project)
        dates_json = await get_available_dates(project_id)
        try:
            dates_data = json.loads(dates_json)
            prefetched["dates"] = dates_data
            logger.info(
                "Pre-fetch: %d dates, request_id=%s",
                len(dates_data.get("available_dates", [])),
                dates_data.get("request_id", "?"),
            )
        except (json.JSONDecodeError, TypeError):
            logger.warning("Pre-fetch: dates not JSON — %s", dates_json[:200])

        # 3. Installation address
        addr_json = await get_installation_address(project_id)
        try:
            addr_data = json.loads(addr_json)
            prefetched["address"] = addr_data.get("address", {})
            logger.info("Pre-fetch: address loaded for project %s", project_id)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Pre-fetch: address not JSON — %s", addr_json[:200])

    except Exception:
        logger.exception("Pre-fetch failed (non-fatal) for project %s", project_id)

    return prefetched


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

    # Extract fields from PF nested payload format
    extracted = _extract_pf_payload(body)
    customer_phone = extracted["customer_phone"]
    customer_phone_alt = extracted["customer_phone_alt"]
    project_id = extracted["project_id"]
    client_id = extracted["client_id"]
    customer_id = extracted["customer_id"]
    customer_name = extracted["customer_name"]
    project_type = extracted["project_type"]
    vapi_phone_number_id = extracted["vapi_phone_number_id"]
    from_phone = extracted["from_phone"]

    if not customer_phone or not project_id:
        logger.error(
            "Missing required fields (phone=%s project=%s) in SQS message %s — deleting",
            customer_phone, project_id, message_id,
        )
        sqs_client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
        return

    logger.info(
        "Processing outbound call: project=%s phone=***%s client=%s from=***%s",
        project_id, customer_phone[-4:], client_id, from_phone[-4:] if from_phone else "default",
    )

    # Guard: vapi_phone_number_id is required for Vapi POST /call
    if not vapi_phone_number_id:
        logger.error(
            "No Vapi phone number ID for outbound call: project=%s client=%s "
            "— neither tenant payload nor config default available. Skipping.",
            project_id, client_id,
        )
        sqs_client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
        return

    # Step 1: Authenticate customer via phone-call-login
    # from_phone (tenant Vapi number) is the to_phone in PF auth API;
    # falls back to config vapi_phone_number if not provided
    if not from_phone:
        logger.info(
            "No tenant_vapi_phone_number in SQS message for project=%s "
            "— using config default for auth",
            project_id,
        )
    system_phone = from_phone or get_settings().vapi_phone_number
    try:
        creds = await get_or_authenticate(customer_phone, system_phone)
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

    # Pre-fetch project data (dates, weather, address) so the AI has it immediately
    resolved_client_id = client_id or creds.get("client_id", "")
    prefetched = await _prefetch_project_data(
        creds=creds,
        project_id=project_id,
        customer_id=customer_id,
        client_id=resolved_client_id,
    )

    # ── Gate: only proceed if project status is schedulable ──
    # PF API may return "Ready to Schedule" even when UI shows "Ready for Auto Call"
    # (race condition from slotsChatbot call). Accept all SCHEDULABLE statuses.
    project_status = (prefetched.get("project", {}).get("status") or "").lower()
    if project_status and project_status not in ProjectStatusRules.SCHEDULABLE:
        logger.info(
            "Skipping outbound call — project %s status '%s' is not schedulable",
            project_id, project_status,
        )
        sqs_client.delete_message(QueueUrl=queue_url, ReceiptHandle=receipt_handle)
        return

    # Dates are prefetched as a nice-to-have (injected into system prompt).
    # If the PF API rejected the request or returned no dates, log it but
    # still make the call — the AI can fetch dates during the conversation.
    dates_data = prefetched.get("dates", {})
    if dates_data.get("already_scheduled"):
        logger.warning(
            "Pre-fetch dates returned 'already_scheduled' for project %s "
            "but status is '%s' — proceeding with call anyway",
            project_id, project_status,
        )
    elif not dates_data.get("available_dates"):
        logger.warning(
            "Pre-fetch returned no dates for project %s — "
            "AI will fetch dates during the conversation",
            project_id,
        )

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
        "prefetched": prefetched,
        "sqs_message_id": message_id,
    }
    call_id = await create_outbound_call(call_data)
    call_data["call_id"] = call_id

    # Step 3: Build assistant config and initiate Vapi call
    try:
        webhook_url = _get_webhook_url()
        assistant_config, server_block = _build_assistant_for_call(call_data)
        vapi_response = await create_vapi_call(
            phone_number_id=vapi_phone_number_id,
            customer_phone=customer_phone,
            customer_name=customer_name,
            server_url=webhook_url,
            assistant_config=assistant_config,
            server_block=server_block,
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
    system_phone = get_settings().vapi_phone_number
    try:
        creds = await get_or_authenticate(alt_phone, system_phone)
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
        retry_call_data = {**outbound_call, "auth_creds": auth_creds}
        assistant_config, server_block = _build_assistant_for_call(retry_call_data)
        vapi_response = await create_vapi_call(
            phone_number_id=outbound_call.get("vapi_phone_number_id", ""),
            customer_phone=alt_phone,
            customer_name=outbound_call.get("customer_name", ""),
            server_url=webhook_url,
            assistant_config=assistant_config,
            server_block=server_block,
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
    """Process an outbound call trigger (from endpoint or SQS-like payload).

    Called by the /outbound/trigger endpoint. Uses the same PF nested
    payload format as SQS messages.

    Returns dict with call_id, vapi_call_id, status.
    """
    extracted = _extract_pf_payload(request_data)
    customer_phone = extracted["customer_phone"]
    project_id = extracted["project_id"]

    # Authenticate — pass system phone as to_phone (PF API requires it)
    system_phone = get_settings().vapi_phone_number
    creds = await get_or_authenticate(customer_phone, system_phone)

    customer_name = extracted["customer_name"] or creds.get("user_name", "")
    customer_id = extracted["customer_id"] or creds.get("customer_id", "")
    client_name = creds.get("client_name", "ProjectsForce")
    resolved_client_id = extracted["client_id"] or creds.get("client_id", "")

    # Pre-fetch project data (dates, weather, address) so the AI has it immediately
    prefetched = await _prefetch_project_data(
        creds=creds,
        project_id=project_id,
        customer_id=customer_id,
        client_id=resolved_client_id,
    )

    # ── Gate: skip call if project is already scheduled or has no dates ──
    dates_data = prefetched.get("dates", {})
    if dates_data.get("already_scheduled"):
        return {"status": "skipped", "reason": "already_scheduled", "project_id": project_id}

    if not dates_data.get("available_dates"):
        return {"status": "skipped", "reason": "no_dates_available", "project_id": project_id}

    call_data = {
        "project_id": project_id,
        "client_id": resolved_client_id,
        "customer_id": customer_id,
        "customer_name": customer_name,
        "client_name": client_name,
        "call_type": "scheduling",
        "phone_primary": customer_phone,
        "phone_alternate": extracted["customer_phone_alt"],
        "phone_used": customer_phone,
        "project_type": extracted["project_type"],
        "vapi_phone_number_id": extracted["vapi_phone_number_id"],
        "auth_creds": {
            "bearer_token": creds.get("bearer_token", ""),
            "client_id": creds.get("client_id", ""),
            "customer_id": creds.get("customer_id", creds.get("user_id", "")),
            "user_id": creds.get("user_id", ""),
            "user_name": creds.get("user_name", ""),
            "timezone": creds.get("timezone", "US/Eastern"),
            "support_number": creds.get("support_number", ""),
        },
        "prefetched": prefetched,
    }
    call_id = await create_outbound_call(call_data)
    call_data["call_id"] = call_id

    # Build assistant config and initiate Vapi call
    webhook_url = _get_webhook_url()
    assistant_config, server_block = _build_assistant_for_call(call_data)
    vapi_response = await create_vapi_call(
        phone_number_id=extracted["vapi_phone_number_id"],
        customer_phone=customer_phone,
        customer_name=customer_name,
        server_url=webhook_url,
        assistant_config=assistant_config,
        server_block=server_block,
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
