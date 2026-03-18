"""Vapi phone channel — POST /vapi/webhook (tool calls + server events).

Vapi handles telephony (phone numbers, SIP, IVR, call routing).  When the
caller asks a question, Vapi's LLM invokes our ``ask_scheduling_bot`` tool.
Vapi sends the tool call to this webhook.  We authenticate the caller via
phone_auth, process the query through the AgentSquad orchestrator, and return
a voice-optimized answer.

Vapi may send tool invocations as ``tool-calls`` (current) or
``function-call`` (legacy) events — both are handled.
"""

import asyncio
import json
import logging
import re
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from auth.context import AuthContext
from auth.phone_auth import get_or_authenticate, get_support_info, normalize_phone
from channels.conversation_log import log_conversation
from channels.formatters import format_for_voice
from channels.vapi_config import get_phone_for_assistant
from config import get_secrets, get_settings
from observability.logging import RequestContext
from orchestrator import get_orchestrator
from orchestrator.response_utils import extract_response_text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vapi", tags=["vapi"])

# Background tasks need a strong reference to avoid GC before completion
_background_tasks: set[asyncio.Task] = set()

# Fallback message spoken to the caller when something goes wrong
_FALLBACK_MESSAGE = (
    "I'm having trouble looking that up right now. "
    "Let me connect you with our support team."
)


# ── Auth dependency ──────────────────────────────────────────────────────


async def verify_vapi_secret(request: Request) -> None:
    """Validate the ``x-vapi-secret`` header against the stored secret."""
    expected = get_secrets().vapi_api_key
    if not expected:
        logger.error("Vapi secret not configured (VAPI_SECRET_ARN empty or unresolvable)")
        raise HTTPException(status_code=401, detail="Unauthorized")

    provided = request.headers.get("x-vapi-secret", "")
    if not provided or provided != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Webhook endpoint ─────────────────────────────────────────────────────


@router.post("/webhook", dependencies=[Depends(verify_vapi_secret)])
async def vapi_webhook(request: Request):
    """Vapi webhook — handles tool calls and server events."""
    body = await request.json()

    # Server URL event: nested "message" dict with "type"
    if isinstance(body.get("message"), dict) and "type" in body["message"]:
        event_type = body["message"].get("type", "")
        if event_type == "tool-calls":
            return await _handle_tool_calls(body)
        if event_type == "function-call":
            return await _handle_function_call(body)
        return _handle_server_event(body)

    # Unrecognized payload — acknowledge to avoid Vapi retries
    logger.warning("Unrecognized Vapi payload keys: %s", list(body.keys()))
    return {"status": "ok"}


# ── Tool calls handler (current Vapi format) ────────────────────────────


async def _handle_tool_calls(body: dict) -> dict:
    """Handle Vapi ``tool-calls`` event — execute tools and return results.

    Vapi sends tool calls in OpenAI format via ``toolCalls`` or ``toolCallList``.
    """
    message = body.get("message", {})
    call_data = message.get("call", body.get("call", {}))
    call_id = call_data.get("id", str(uuid.uuid4()))

    # Extract tool call list from whichever key Vapi uses
    tool_call_list = message.get("toolCalls", message.get("toolCallList", []))
    if not tool_call_list:
        for item in message.get("toolWithToolCallList", []):
            tc = item.get("toolCall", {})
            tool_call_list.append(
                {
                    "id": tc.get("id", ""),
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": tc.get("parameters", {}),
                    },
                }
            )

    if not tool_call_list:
        logger.warning("Empty toolCalls in tool-calls event (call_id=%s)", call_id)
        return {"results": []}

    # Process the first tool call
    tc = tool_call_list[0]
    tool_call_id = tc.get("id", "")

    fn = tc.get("function", {})
    tool_name = fn.get("name", "") or tc.get("name", "")
    args = fn.get("arguments", tc.get("parameters", {}))
    if isinstance(args, str):
        try:
            tool_params = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            tool_params = {}
    elif isinstance(args, dict):
        tool_params = args
    else:
        tool_params = {}

    # Derive session/user identifiers from call metadata
    session_id = f"vapi-{call_id}"
    phone_number = _extract_phone_number(call_data)
    user_id = phone_number or "vapi-anonymous"

    RequestContext.set(session_id=session_id, user_id=user_id, channel="vapi")
    logger.info(
        "Vapi tool-calls: call_id=%s tool=%s toolCallId=%s",
        call_id,
        tool_name,
        tool_call_id,
    )

    # Authenticate caller and set auth context
    await _set_auth_context_from_phone(call_data, session_id)

    return await _process_tool(tool_name, tool_params, user_id, session_id, call_id, tool_call_id)


# ── Function call handler (legacy Vapi format) ──────────────────────────


async def _handle_function_call(body: dict) -> dict:
    """Handle Vapi ``function-call`` event (legacy format)."""
    message = body.get("message", {})
    call_data = message.get("call", body.get("call", {}))
    call_id = call_data.get("id", str(uuid.uuid4()))

    function_call = message.get("functionCall", {})
    tool_name = function_call.get("name", "")
    tool_params = function_call.get("parameters", {})
    tool_call_id = message.get("toolCallId", "")

    session_id = f"vapi-{call_id}"
    phone_number = _extract_phone_number(call_data)
    user_id = phone_number or "vapi-anonymous"

    RequestContext.set(session_id=session_id, user_id=user_id, channel="vapi")
    logger.info(
        "Vapi function-call: call_id=%s tool=%s toolCallId=%s",
        call_id,
        tool_name,
        tool_call_id,
    )

    # Authenticate caller and set auth context
    await _set_auth_context_from_phone(call_data, session_id)

    return await _process_tool(tool_name, tool_params, user_id, session_id, call_id, tool_call_id)


# ── Server URL event handler ────────────────────────────────────────────


def _handle_server_event(body: dict) -> dict:
    """Handle Vapi Server URL events (end-of-call-report, status-update, etc.)."""
    message = body.get("message", {})
    event_type = message.get("type", "unknown")
    call_data = message.get("call", body.get("call", {}))
    call_id = call_data.get("id", "unknown")

    if event_type == "end-of-call-report":
        reason = message.get("endedReason", "unknown")
        summary = message.get("summary", "")
        cost = message.get("cost", 0)
        logger.info(
            "Vapi end-of-call: call_id=%s reason=%s cost=%s summary=%s",
            call_id,
            reason,
            cost,
            summary[:200] if summary else "",
        )
    elif event_type == "status-update":
        status = message.get("status", "unknown")
        logger.info("Vapi status-update: call_id=%s status=%s", call_id, status)
    else:
        logger.info("Vapi event: call_id=%s type=%s", call_id, event_type)

    return {"status": "ok"}


# ── Helpers ──────────────────────────────────────────────────────────────


async def _process_tool(
    tool_name: str,
    tool_params: dict,
    user_id: str,
    session_id: str,
    call_id: str,
    tool_call_id: str,
) -> dict:
    """Route a tool invocation to the orchestrator and return a Vapi result."""
    if tool_name == "ask_scheduling_bot":
        # Handle support number request directly — no need for orchestrator
        action = tool_params.get("action", "")
        if action == "send_support_sms":
            return _handle_support_request(user_id, tool_call_id)

        question = tool_params.get("question", "")
        if not question:
            logger.warning("Empty question in %s (call_id=%s)", tool_name, call_id)
            return _build_tool_result(_FALLBACK_MESSAGE, tool_call_id)

        agent_name = ""
        start_time = time.monotonic()
        try:
            orchestrator = get_orchestrator()
            response = await orchestrator.route_request(
                user_input=question,
                user_id=user_id,
                session_id=session_id,
                additional_params={"channel": "vapi"},
            )
            response_text = extract_response_text(response.output)
            voice_text = format_for_voice(response_text)
            agent_name = response.metadata.agent_name if response.metadata else ""
        except Exception:
            logger.exception("Orchestrator error during Vapi call (call_id=%s)", call_id)
            voice_text = _FALLBACK_MESSAGE
        elapsed_ms = int((time.monotonic() - start_time) * 1000)

        logger.info("Vapi tool result: call_id=%s chars=%d elapsed_ms=%d", call_id, len(voice_text), elapsed_ms)
        task = asyncio.create_task(
            log_conversation(
                session_id=session_id,
                user_id=user_id,
                user_message=question,
                bot_response=voice_text,
                agent_name=agent_name,
                channel="vapi",
                response_time_ms=elapsed_ms,
            )
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return _build_tool_result(voice_text, tool_call_id)

    logger.warning("Unknown tool: %s (call_id=%s)", tool_name, call_id)
    return _build_tool_result(f"Unknown tool: {tool_name}", tool_call_id)


async def _set_auth_context_from_phone(call_data: dict, session_id: str) -> None:
    """Authenticate the caller via phone number and populate AuthContext."""
    from_phone = _extract_phone_number(call_data)
    to_phone = _resolve_to_phone(call_data)

    if not from_phone:
        logger.warning("No caller phone number — skipping phone auth for session %s", session_id)
        return

    try:
        creds = await get_or_authenticate(from_phone, to_phone)
        AuthContext.set(
            auth_token=creds.get("bearer_token", ""),
            client_id=creds.get("client_id", ""),
            customer_id=creds.get("customer_id", creds.get("user_id", "")),
            user_id=creds.get("user_id", ""),
            user_name=creds.get("user_name", ""),
        )
    except Exception:
        logger.exception("Phone auth failed for session %s", session_id)


def _extract_phone_number(call_data: dict) -> str:
    """Extract the caller's phone number from Vapi call metadata."""
    phone = call_data.get("customer", {}).get("number", "")
    if not phone:
        phone_obj = call_data.get("phoneNumber")
        if isinstance(phone_obj, dict):
            phone = phone_obj.get("number", "")
        elif isinstance(phone_obj, str):
            phone = phone_obj
    return phone


def _resolve_to_phone(call_data: dict) -> str:
    """Resolve the destination phone number from call data.

    Fallback chain:
    1. ``call_data.phoneNumber`` — populated for Twilio/Vonage numbers
    2. Vapi assistant config table — maps ``assistantId`` → phone number
    3. ``VAPI_PHONE_NUMBER`` env var — legacy single-tenant fallback
    """
    # 1. Direct from call data (Twilio/Vonage-backed Vapi numbers)
    phone_obj = call_data.get("phoneNumber")
    if isinstance(phone_obj, dict):
        phone = phone_obj.get("number", "")
        if phone:
            return phone
    elif isinstance(phone_obj, str) and phone_obj:
        return phone_obj

    # 2. Look up by assistant ID (multi-tenant, Vapi-managed numbers)
    assistant_id = call_data.get("assistantId", "")
    if assistant_id:
        phone = get_phone_for_assistant(assistant_id)
        if phone:
            return phone

    # 3. Legacy env var fallback
    return get_settings().vapi_phone_number


def _handle_support_request(user_id: str, tool_call_id: str) -> dict:
    """Return the tenant's support phone number from cached credentials."""
    phone = normalize_phone(user_id)
    info = get_support_info(phone)
    support_number = info.get("support_number", "")
    support_email = info.get("support_email", "")
    client_name = info.get("client_name", "ProjectsForce")

    if support_number:
        # Format phone number for voice readout (digit by digit with pauses)
        digits = "".join(ch for ch in support_number if ch.isdigit())
        if len(digits) == 10:
            voice_number = (
                f"{_digit_word(digits[0])}, {_digit_word(digits[1])}, {_digit_word(digits[2])}... "
                f"{_digit_word(digits[3])}, {_digit_word(digits[4])}, {_digit_word(digits[5])}... "
                f"{_digit_word(digits[6])}, {_digit_word(digits[7])}, {_digit_word(digits[8])}, {_digit_word(digits[9])}"
            )
        elif len(digits) == 11 and digits.startswith("1"):
            d = digits[1:]
            voice_number = (
                f"{_digit_word(d[0])}, {_digit_word(d[1])}, {_digit_word(d[2])}... "
                f"{_digit_word(d[3])}, {_digit_word(d[4])}, {_digit_word(d[5])}... "
                f"{_digit_word(d[6])}, {_digit_word(d[7])}, {_digit_word(d[8])}, {_digit_word(d[9])}"
            )
        else:
            voice_number = support_number

        msg = f"You can reach {client_name} at {voice_number}."
        if support_email:
            msg += f" You can also email them at {support_email}."
    else:
        msg = (
            "I don't have the office number on file right now. "
            "Please check your project documents or the ProjectsForce app for contact information."
        )

    logger.info("Support request: number=%s email=%s", support_number[-4:] if support_number else "none", support_email or "none")
    return _build_tool_result(msg, tool_call_id)


_DIGIT_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]


def _digit_word(d: str) -> str:
    """Convert a single digit character to its word form."""
    return _DIGIT_WORDS[int(d)] if d.isdigit() else d


def _build_tool_result(text: str, tool_call_id: str = "") -> dict:
    """Build a Vapi tool-result response with single-line text."""
    single_line = re.sub(r"\s*\n\s*", " ", text).strip()
    result_entry: dict = {"result": single_line}
    if tool_call_id:
        result_entry["toolCallId"] = tool_call_id
    return {"results": [result_entry]}
