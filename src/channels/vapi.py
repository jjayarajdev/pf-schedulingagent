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
from auth.phone_auth import (
    AuthenticationError,
    authenticate_store,
    get_cached_auth,
    get_or_authenticate,
    normalize_phone,
)
from channels.conversation_log import log_conversation
from channels.formatters import format_for_voice
from channels.vapi_config import get_phone_for_assistant
from config import get_secrets, get_settings
from observability.logging import RequestContext
from orchestrator import get_orchestrator
from orchestrator.response_utils import extract_response_text
from tools.pii_filter import scrub_pii
from tools.scheduling import clear_session_projects, post_call_summary_notes, post_store_call_notes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vapi", tags=["vapi"])

# Background tasks need a strong reference to avoid GC before completion
_background_tasks: set[asyncio.Task] = set()

# Fallback message spoken to the caller when something goes wrong
_FALLBACK_MESSAGE = (
    "I'm having trouble looking that up right now. "
    "Let me connect you with our support team."
)

# Store caller sessions: call_id → {to_phone, creds, authenticated}
# Tracks auth state across tool calls within one Vapi call.
_store_sessions: dict[str, dict] = {}


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


@router.post("/webhook")
async def vapi_webhook(request: Request):
    """Vapi webhook — handles tool calls and server events.

    Auth is enforced for all events except ``assistant-request``, which
    arrives at call start before Vapi has the assistant's server secret.
    The returned assistant config includes ``server.secret`` so all
    subsequent events (tool calls, status updates) are authenticated.
    """
    body = await request.json()

    # Server URL event: nested "message" dict with "type"
    if isinstance(body.get("message"), dict) and "type" in body["message"]:
        event_type = body["message"].get("type", "")

        # assistant-request arrives before Vapi knows the server secret —
        # skip auth here; the response includes server.secret for future calls
        if event_type == "assistant-request":
            return await _handle_assistant_request(body)

        # All other events require auth
        await verify_vapi_secret(request)

        if event_type == "tool-calls":
            return await _handle_tool_calls(body)
        if event_type == "function-call":
            return await _handle_function_call(body)
        return _handle_server_event(body)

    # Unrecognized payload — still require auth
    await verify_vapi_secret(request)
    logger.warning("Unrecognized Vapi payload keys: %s", list(body.keys()))
    return {"status": "ok"}


# ── Assistant request handler (dynamic greeting at call start) ───────────


async def _handle_assistant_request(body: dict) -> dict:
    """Handle Vapi ``assistant-request`` — return full assistant config with personalized greeting.

    Vapi sends this at call start when the phone number uses server-URL mode
    (no fixed assistantId).  We authenticate the caller by phone number to get
    their name, then return the complete assistant configuration with a
    ``firstMessage`` that includes the caller's first name.

    If phone-call-login fails (non-200), the caller is treated as a store caller
    and gets a store-specific assistant config that asks for a PO/project lookup
    value before proceeding.
    """
    message = body.get("message", {})
    call_data = message.get("call", body.get("call", {}))
    call_id = call_data.get("id", "unknown")

    from_phone = _extract_phone_number(call_data)
    to_phone = _resolve_to_phone(call_data)

    logger.info("Vapi assistant-request: call_id=%s from=***%s", call_id, from_phone[-4:] if from_phone else "none")

    webhook_secret = get_secrets().vapi_api_key
    session_key = f"vapi-{call_id}"

    # Authenticate caller to get their name
    is_store_caller = False
    first_name = ""
    client_name = "ProjectsForce"
    support_number = ""
    if from_phone:
        try:
            creds = await get_or_authenticate(from_phone, to_phone)
            user_name = creds.get("user_name", "")
            first_name = user_name.split()[0] if user_name and user_name.strip() else ""
            client_name = creds.get("client_name", "ProjectsForce") or "ProjectsForce"
            support_number = creds.get("support_number", "")
        except AuthenticationError as exc:
            # Auth failure = potential store caller
            logger.info("Store caller detected (call_id=%s)", call_id)
            is_store_caller = True
            # PF API may return client_name even on auth failure
            if exc.client_name:
                client_name = exc.client_name
        except Exception:
            logger.exception("Phone auth failed during assistant-request (call_id=%s)", call_id)

    if is_store_caller:
        _store_sessions[session_key] = {"to_phone": to_phone, "authenticated": False}
        greeting = _generate_store_greeting(client_name)
        logger.info("Vapi store greeting: call_id=%s client=%s", call_id, client_name)
        return {"assistant": _build_store_assistant_config(greeting, webhook_secret, client_name)}

    greeting = _generate_dynamic_greeting(first_name, client_name)
    logger.info(
        "Vapi dynamic greeting: call_id=%s name=%s client=%s",
        call_id,
        first_name or "(anonymous)",
        client_name,
    )

    return {"assistant": _build_assistant_config(greeting, webhook_secret, support_number, client_name)}


def _generate_dynamic_greeting(first_name: str, client_name: str) -> str:
    """Build a personalized SSML greeting with the caller's first name.

    Returns an SSML string with ``<break>`` pauses for natural pacing:
    - 3s initial pause (call connection settling)
    - 300ms after name
    - 500ms after intro
    """
    name_part = f"Hello {first_name}!" if first_name else "Hello!"
    intro = f"I'm J, your AI assistant from {client_name}."
    guidance = (
        "I can help you view your projects, check available dates, "
        "or schedule appointments. What would you like to do today?"
    )
    return (
        f'<break time="3000ms"/> {name_part} <break time="300ms"/> '
        f'{intro} <break time="500ms"/> '
        f'{guidance}'
    )


def _normalize_e164(phone: str) -> str:
    """Normalize a phone number to E.164 format (+1XXXXXXXXXX)."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}"


def _transfer_call_tool(support_number: str, client_name: str = "ProjectsForce") -> list[dict]:
    """Build a Vapi ``transferCall`` tool for warm transfer (experimental mode).

    Uses ``warm-transfer-experimental`` with a dedicated transfer assistant
    that speaks a conversation summary to the support agent before connecting
    the caller.  This mode supports Vapi-native phone numbers (no Twilio
    requirement).

    Returns a list with one tool dict if ``support_number`` is provided,
    or an empty list (so it can be unpacked with ``*`` into the tools array).
    """
    if not support_number:
        return []

    name = client_name or "ProjectsForce"
    e164 = _normalize_e164(support_number)

    return [
        {
            "type": "transferCall",
            "messages": [
                {
                    "type": "request-start",
                    "content": (
                        "I'm transferring you to our support team now. "
                        "Please hold while I connect you."
                    ),
                },
                {
                    "type": "request-complete",
                    "content": "https://desert-horse-9859.twil.io/assets/soothing-sound.mp3",
                },
                {
                    "type": "request-failed",
                    "content": (
                        "I wasn't able to reach our support team right now. "
                        "Is there anything else I can help you with?"
                    ),
                },
            ],
            "destinations": [
                {
                    "type": "number",
                    "number": e164,
                    "transferPlan": {
                        "mode": "warm-transfer-experimental",
                        "transferAssistant": {
                            "firstMessage": (
                                f"Hi, this is J from {name}. "
                                "I have a customer on hold. Quick brief."
                            ),
                            "firstMessageMode": "assistant-speaks-first",
                            "maxDurationSeconds": 60,
                            "silenceTimeoutSeconds": 15,
                            "model": {
                                "provider": "openai",
                                "model": "gpt-4o-mini",
                                "messages": [
                                    {
                                        "role": "system",
                                        "content": (
                                            "You are a call transfer assistant. Be VERY brief.\n\n"
                                            "After your greeting, give a 2-3 sentence summary:\n"
                                            "- Caller name\n"
                                            "- What they need (one line)\n"
                                            "- Then ask: 'Ready to connect?'\n\n"
                                            "Do NOT retell the whole conversation. "
                                            "Do NOT list every project detail. "
                                            "Just the essentials — the customer is waiting.\n\n"
                                            "If agent says yes → call transferSuccessful.\n"
                                            "If agent says no → call transferCancel."
                                        ),
                                    },
                                ],
                                "tools": [
                                    {
                                        "type": "transferSuccessful",
                                        "function": {
                                            "name": "transferSuccessful",
                                            "description": "Connect the customer when the agent is ready.",
                                        },
                                        "messages": [
                                            {
                                                "type": "request-start",
                                                "content": "Connecting the customer now.",
                                            },
                                        ],
                                    },
                                    {
                                        "type": "transferCancel",
                                        "function": {
                                            "name": "transferCancel",
                                            "description": "Cancel the transfer if the agent is unavailable.",
                                        },
                                        "messages": [
                                            {
                                                "type": "request-complete",
                                                "content": "I'll let the customer know.",
                                            },
                                        ],
                                    },
                                ],
                            },
                        },
                    },
                }
            ],
        }
    ]


def _build_assistant_config(
    first_message: str,
    server_secret: str = "",
    support_number: str = "",
    client_name: str = "ProjectsForce",
) -> dict:
    """Build the full Vapi assistant config returned on ``assistant-request``.

    This mirrors the assistant settings previously stored in Vapi's dashboard
    but with a dynamic ``firstMessage``.
    """
    name = client_name or "ProjectsForce"
    server_config: dict = {
        "url": "https://schedulingagent.dev.projectsforce.com/vapi/webhook",
        "timeoutSeconds": 30,
    }
    if server_secret:
        server_config["secret"] = server_secret
    return {
        "name": f"{name} Scheduling Bot",
        "voice": {
            "model": "sonic-3",
            "voiceId": "829ccd10-f8b3-43cd-b8a0-4aeaa81f3b30",
            "provider": "cartesia",
        },
        "model": {
            "model": "gpt-4o-mini",
            "provider": "openai",
            "temperature": 0.3,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"You are J, a friendly phone assistant for {name} "
                        "— a home improvement scheduling service.\n\n"
                        "CRITICAL RULES:\n"
                        '1. You MUST call ask_scheduling_bot for EVERY user request — no exceptions. '
                        "You cannot answer any question about projects, scheduling, dates, times, "
                        "appointments, or any follow-up from memory.\n"
                        "2. Pass the user's EXACT words in the \"question\" field. Do NOT rephrase, "
                        "summarize, or add your own questions.\n"
                        "3. NEVER ask clarifying questions before calling the tool. Let the scheduling "
                        "bot handle clarification — it knows the user's projects and context.\n"
                        "4. When the tool returns a response, speak it naturally to the user. "
                        "Keep it conversational — you are on a phone call.\n"
                        "5. For multi-step flows (scheduling, rescheduling), call ask_scheduling_bot "
                        "for EVERY step. The bot maintains conversation context.\n"
                        "6. If the user asks to speak to a person, transfer the call, or wants human support, "
                        "use the transferCall tool to connect them. Do NOT read out a phone number.\n"
                        "7. Only handle basic greetings and goodbyes yourself. "
                        "Everything else goes through ask_scheduling_bot.\n"
                        "8. Keep your spoken responses concise — this is a phone call, not a text chat. "
                        "No bullet points, no markdown.\n"
                        "9. Use natural filler phrases while waiting: "
                        '"Let me check that for you", "One moment please".\n'
                        "10. If the tool call fails, say: "
                        '"I\'m having trouble looking that up. Let me try again." and retry once.\n'
                        "11. CRITICAL — CONFIRMATION COMPLETES THE BOOKING: When the scheduling bot "
                        'asks the user to confirm (e.g., "Should I go ahead?", "Shall I book this?"), '
                        "the user's reply (yes, sure, go ahead, confirm, etc.) MUST be passed back "
                        "to ask_scheduling_bot. The booking is NOT complete until the bot processes "
                        "the confirmation. NEVER end the call or assume the appointment is booked — "
                        "only the scheduling bot can finalize it.\n"
                        "12. Do NOT end the call until the scheduling bot has confirmed the booking "
                        "is complete OR the user explicitly says goodbye/bye/that's all I need."
                    ),
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "ask_scheduling_bot",
                        "description": (
                            "REQUIRED for ALL user requests. Call this tool for every question, "
                            "request, or follow-up about projects, scheduling, appointments, "
                            "dates, times, rescheduling, cancellation, notes, weather, or "
                            "anything related to their service. The bot has full context about "
                            "the caller's account and projects. NEVER try to answer without "
                            "calling this tool first."
                        ),
                        "parameters": {
                            "type": "object",
                            "required": ["question"],
                            "properties": {
                                "question": {
                                    "type": "string",
                                    "description": (
                                        "The user's EXACT words. Pass verbatim what they said "
                                        "— do not rephrase or add context."
                                    ),
                                },
                            },
                        },
                    },
                    "messages": [
                        {"type": "request-start", "content": "Sure, let me check."},
                        {
                            "type": "request-response-delayed",
                            "content": "Still working on that.",
                            "timingMilliseconds": 3000,
                        },
                        {
                            "type": "request-response-delayed",
                            "content": "Almost there.",
                            "timingMilliseconds": 5000,
                        },
                        {
                            "type": "request-failed",
                            "content": "I had some trouble with that. Could you try asking again?",
                        },
                    ],
                },
                *(_transfer_call_tool(support_number, name)),
            ],
        },
        "transcriber": {
            "model": "nova-3",
            "language": "en",
            "provider": "deepgram",
            "endpointing": 150,
        },
        "firstMessage": first_message,
        "endCallMessage": (
            f"Thank you for calling {name}. "
            "Your scheduling is all set. Have a wonderful day!"
        ),
        "endCallPhrases": [
            "goodbye", "bye", "bye bye", "bye now",
            "talk to you later", "have a great day",
            "have a good day",
        ],
        "endCallFunctionEnabled": True,
        "voicemailMessage": (
            f"Hello, this is J from {name}. I'm calling about your "
            "home improvement project. Please call us back at your earliest convenience."
        ),
        "silenceTimeoutSeconds": 30,
        "maxDurationSeconds": 600,
        "backgroundDenoisingEnabled": True,
        "startSpeakingPlan": {
            "waitSeconds": 0.4,
            "smartEndpointingEnabled": True,
        },
        "hipaaEnabled": False,
        "server": server_config,
        "serverUrl": "https://schedulingagent.dev.projectsforce.com/vapi/webhook",
    }


def _generate_store_greeting(client_name: str = "ProjectsForce") -> str:
    """Build an SSML greeting for store callers."""
    name = client_name or "ProjectsForce"
    return (
        f'<break time="3000ms"/> Welcome to {name}. '
        '<break time="300ms"/> '
        "I can help you check project status. "
        '<break time="500ms"/> '
        "Do you have a PO number, project number, or customer name?"
    )


def _build_store_assistant_config(
    first_message: str, server_secret: str = "", client_name: str = "ProjectsForce",
) -> dict:
    """Build the Vapi assistant config for store callers.

    Uses ``ask_store_bot`` tool instead of ``ask_scheduling_bot``.
    The LLM first collects a lookup value, then routes all queries through
    the same orchestrator with store-specific auth.
    """
    name = client_name or "ProjectsForce"
    server_config: dict = {
        "url": "https://schedulingagent.dev.projectsforce.com/vapi/webhook",
        "timeoutSeconds": 30,
    }
    if server_secret:
        server_config["secret"] = server_secret
    return {
        "name": f"{name} Store Bot",
        "voice": {
            "model": "sonic-3",
            "voiceId": "829ccd10-f8b3-43cd-b8a0-4aeaa81f3b30",
            "provider": "cartesia",
        },
        "model": {
            "model": "gpt-4o-mini",
            "provider": "openai",
            "temperature": 0.3,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"You are J, a friendly phone assistant for {name} "
                        "— a home improvement scheduling service.\n\n"
                        "The caller is from a STORE (not a customer).\n\n"
                        "CRITICAL RULES:\n"
                        "1. First, ask for a PO number, project number, or customer name "
                        "to look up the account.\n"
                        "2. Call ask_store_bot with the lookup info. On the first call you "
                        "MUST include lookup_type and lookup_value.\n"
                        "3. Once authenticated, use ask_store_bot for ALL queries. "
                        "You do NOT need to pass lookup_type/lookup_value again after the first call.\n"
                        "4. Pass the user's EXACT words in the \"question\" field. Do NOT "
                        "rephrase, summarize, or add your own questions.\n"
                        "5. NEVER share customer names, technician names, phone numbers, "
                        "email addresses, or street addresses with the store caller. "
                        "Only share project status, project numbers, and PO numbers.\n"
                        "6. NEVER offer to schedule, reschedule, or cancel appointments. "
                        "Store callers can ONLY check project status. If they ask to schedule, "
                        "say: 'Scheduling is not available for store calls. "
                        "Please have the customer call us directly.'\n"
                        "7. NEVER ask clarifying questions before calling the tool. Let the "
                        "scheduling bot handle clarification.\n"
                        "8. When the tool returns a response, speak it naturally. "
                        "Keep it conversational — you are on a phone call. "
                        "Remove any customer names or technician names from the response.\n"
                        "9. Keep your spoken responses concise — no bullet points, "
                        "no markdown.\n"
                        "10. Use natural filler phrases while waiting: "
                        '"Let me check that for you", "One moment please".'
                    ),
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "ask_store_bot",
                        "description": (
                            "REQUIRED for ALL requests. On first call, include lookup_type "
                            "and lookup_value to authenticate. After that, only question is "
                            "needed for scheduling queries."
                        ),
                        "parameters": {
                            "type": "object",
                            "required": ["question"],
                            "properties": {
                                "question": {
                                    "type": "string",
                                    "description": (
                                        "The user's EXACT words. Pass verbatim what they said."
                                    ),
                                },
                                "lookup_type": {
                                    "type": "string",
                                    "enum": [
                                        "project_number",
                                        "po_number",
                                        "customer_name",
                                    ],
                                    "description": (
                                        "Type of lookup value. Required on first call."
                                    ),
                                },
                                "lookup_value": {
                                    "type": "string",
                                    "description": (
                                        "The PO number, project number, or customer name. "
                                        "Required on first call."
                                    ),
                                },
                            },
                        },
                    },
                    "messages": [
                        {"type": "request-start", "content": "Let me look that up for you."},
                        {
                            "type": "request-response-delayed",
                            "content": "Still working on that.",
                            "timingMilliseconds": 3000,
                        },
                        {
                            "type": "request-response-delayed",
                            "content": "Almost there.",
                            "timingMilliseconds": 5000,
                        },
                        {
                            "type": "request-failed",
                            "content": "I had some trouble with that. Could you try again?",
                        },
                    ],
                }
            ],
        },
        "transcriber": {
            "model": "nova-3",
            "language": "en",
            "provider": "deepgram",
            "endpointing": 150,
        },
        "firstMessage": first_message,
        "endCallMessage": (
            f"Thank you for calling {name}. Have a great day!"
        ),
        "endCallPhrases": [
            "goodbye", "bye", "bye bye", "bye now",
            "talk to you later", "have a great day",
            "have a good day",
        ],
        "endCallFunctionEnabled": True,
        "silenceTimeoutSeconds": 30,
        "maxDurationSeconds": 600,
        "backgroundDenoisingEnabled": True,
        "startSpeakingPlan": {
            "waitSeconds": 0.4,
            "smartEndpointingEnabled": True,
        },
        "hipaaEnabled": False,
        "server": server_config,
        "serverUrl": "https://schedulingagent.dev.projectsforce.com/vapi/webhook",
    }


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
        duration = message.get("durationSeconds", 0)
        logger.info(
            "Vapi end-of-call: call_id=%s reason=%s cost=%s duration=%ss summary=%s",
            call_id,
            reason,
            cost,
            duration,
            summary[:200] if summary else "",
        )

        # Post call summary notes to discussed projects (fire-and-forget)
        session_id = f"vapi-{call_id}"
        session_key = f"vapi-{call_id}"
        store_session = _store_sessions.get(session_key)

        if store_session and store_session.get("authenticated") and summary:
            # Store call — use /authentication/add-note endpoint
            store_creds = store_session["creds"]
            task = asyncio.create_task(
                post_store_call_notes(
                    session_id=session_id,
                    bearer_token=store_creds.get("bearer_token", ""),
                    client_id=store_creds.get("client_id", ""),
                    summary=summary,
                    duration_seconds=duration,
                )
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
        else:
            # Customer call — use /communication/.../note endpoint
            phone_number = _extract_phone_number(call_data)
            if phone_number and summary:
                creds = get_cached_auth(normalize_phone(phone_number))
                if creds and creds.get("bearer_token"):
                    task = asyncio.create_task(
                        post_call_summary_notes(
                            session_id=session_id,
                            bearer_token=creds["bearer_token"],
                            client_id=creds["client_id"],
                            customer_id=creds["customer_id"],
                            summary=summary,
                            duration_seconds=duration,
                        )
                    )
                    _background_tasks.add(task)
                    task.add_done_callback(_background_tasks.discard)
                else:
                    logger.warning(
                        "No cached creds for call notes (call_id=%s phone=***%s)",
                        call_id, phone_number[-4:] if phone_number else "none",
                    )
                    clear_session_projects(session_id)
            else:
                clear_session_projects(session_id)

        # Clean up store session
        _store_sessions.pop(session_key, None)
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

    if tool_name == "ask_store_bot":
        return await _handle_store_bot(
            tool_params, user_id, session_id, call_id, tool_call_id,
        )

    logger.warning("Unknown tool: %s (call_id=%s)", tool_name, call_id)
    return _build_tool_result(f"Unknown tool: {tool_name}", tool_call_id)


async def _handle_store_bot(
    tool_params: dict,
    user_id: str,
    session_id: str,
    call_id: str,
    tool_call_id: str,
) -> dict:
    """Handle ``ask_store_bot`` tool — authenticate store caller and route queries."""
    question = tool_params.get("question", "")
    lookup_type = tool_params.get("lookup_type", "")
    lookup_value = tool_params.get("lookup_value", "")

    # STT often transcribes dictated numbers with spaces ("5 2 3 8 2 4").
    # Strip spaces for numeric lookup types so the PF API can match.
    if lookup_value and lookup_type in ("po_number", "project_number"):
        lookup_value = lookup_value.replace(" ", "")

    session_key = f"vapi-{call_id}"
    store_session = _store_sessions.get(session_key, {})

    # Step 1: Authenticate if not already
    if not store_session.get("authenticated"):
        if not lookup_type or not lookup_value:
            return _build_tool_result(
                "I need a PO number, project number, or customer name to look you up. "
                "Which one do you have?",
                tool_call_id,
            )
        to_phone = store_session.get("to_phone", "")
        tenant_phone = normalize_phone(to_phone) if to_phone else ""
        try:
            creds = await authenticate_store(tenant_phone, lookup_type, lookup_value)
        except AuthenticationError as exc:
            logger.warning(
                "Store auth failed: call_id=%s lookup=%s:%s error=%s",
                call_id, lookup_type, lookup_value, exc,
            )
            return _build_tool_result(
                "I couldn't find an account with that information. "
                "Could you double-check and try again?",
                tool_call_id,
            )
        store_session["creds"] = creds
        store_session["authenticated"] = True
        _store_sessions[session_key] = store_session

    if not question:
        return _build_tool_result(
            "You're verified! How can I help you? "
            "I can look up project status and details.",
            tool_call_id,
        )

    # Step 2: Set AuthContext from cached store creds
    creds = store_session["creds"]
    AuthContext.set(
        auth_token=creds.get("bearer_token", ""),
        client_id=creds.get("client_id", ""),
        customer_id=creds.get("customer_id", creds.get("user_id", "")),
        user_id=creds.get("user_id", ""),
        user_name=creds.get("user_name", ""),
        caller_type="store",
        tenant_phone=normalize_phone(store_session.get("to_phone", "")),
    )

    # Step 3: Route through orchestrator with store context
    # Prepend store instruction so the scheduling agent restricts its response
    store_question = (
        "[STORE CALLER — status only, no scheduling, no customer/technician names] "
        + question
    )
    agent_name = ""
    start_time = time.monotonic()
    try:
        orchestrator = get_orchestrator()
        response = await orchestrator.route_request(
            user_input=store_question,
            user_id=user_id,
            session_id=session_id,
            additional_params={"channel": "vapi", "caller_type": "store"},
        )
        response_text = extract_response_text(response.output)
        agent_name = response.metadata.agent_name if response.metadata else ""
    except Exception:
        logger.exception("Orchestrator error during store call (call_id=%s)", call_id)
        response_text = _FALLBACK_MESSAGE
    elapsed_ms = int((time.monotonic() - start_time) * 1000)

    # Step 4: PII scrub + voice format
    voice_text = format_for_voice(scrub_pii(response_text))

    logger.info(
        "Vapi store tool result: call_id=%s chars=%d elapsed_ms=%d",
        call_id, len(voice_text), elapsed_ms,
    )
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


async def _set_auth_context_from_phone(call_data: dict, session_id: str) -> None:
    """Authenticate the caller via phone number and populate AuthContext.

    For store callers (tracked in ``_store_sessions``), uses cached store
    credentials instead of calling phone-call-login (which would fail again).
    """
    call_id = call_data.get("id", "")
    session_key = f"vapi-{call_id}"

    # If this is an authenticated store session, use cached creds
    store_session = _store_sessions.get(session_key)
    if store_session and store_session.get("authenticated"):
        creds = store_session["creds"]
        AuthContext.set(
            auth_token=creds.get("bearer_token", ""),
            client_id=creds.get("client_id", ""),
            customer_id=creds.get("customer_id", creds.get("user_id", "")),
            user_id=creds.get("user_id", ""),
            user_name=creds.get("user_name", ""),
            caller_type="store",
            tenant_phone=normalize_phone(store_session.get("to_phone", "")),
        )
        return

    # If this is an unauthenticated store session, skip (ask_store_bot will handle it)
    if store_session:
        return

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


def _build_tool_result(text: str, tool_call_id: str = "") -> dict:
    """Build a Vapi tool-result response with single-line text."""
    single_line = re.sub(r"\s*\n\s*", " ", text).strip()
    result_entry: dict = {"result": single_line}
    if tool_call_id:
        result_entry["toolCallId"] = tool_call_id
    return {"results": [result_entry]}
