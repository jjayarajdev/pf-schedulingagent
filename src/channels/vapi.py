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
from auth.office_hours import check_office_hours
from auth.phone_auth import (
    AuthenticationError,
    authenticate_store,
    get_cached_auth,
    get_or_authenticate,
    normalize_phone,
)
from channels.conversation_log import log_conversation
from channels.formatters import format_for_voice
from channels.vapi_config import get_assistant_info, get_phone_for_assistant
from config import get_secrets, get_settings
from observability.logging import RequestContext
from orchestrator import get_orchestrator
from orchestrator.response_utils import extract_response_text
from tools.pii_filter import scrub_pii
from tools.scheduling import (
    clear_session_projects,
    post_call_summary_notes,
    post_store_call_notes,
    reset_confirm_flag,
    was_confirm_called,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vapi", tags=["vapi"])

# Environment-aware webhook URL (Vapi sends tool calls back here)
_WEBHOOK_URLS = {
    "dev": "https://schedulingagent.dev.projectsforce.com/vapi/webhook",
    "qa": "https://schedulingagent.qa.projectsforce.com/vapi/webhook",
    "staging": "https://schedulingagent.staging.projectsforce.com/vapi/webhook",
    "prod": "https://schedulingagent.apps.projectsforce.com/vapi/webhook",
}


def _get_webhook_url() -> str:
    env = get_settings().environment
    return _WEBHOOK_URLS.get(env, _WEBHOOK_URLS["dev"])


# Shared voice config — used by main assistant, store assistant, AND transfer assistant
# so the caller hears a consistent voice throughout the entire call.
_VOICE_CONFIG = {
    "provider": "cartesia",
    "model": "sonic-3",
    "voiceId": "829ccd10-f8b3-43cd-b8a0-4aeaa81f3b30",
}

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

# Patterns the scheduling agent uses when it hallucinated a booking confirmation
# without actually calling confirm_appointment.
_BOOKING_CONFIRMATION_PATTERNS = [
    "appointment confirmed",
    "appointment is now confirmed",
    "is now scheduled",
    "has been scheduled",
    "has been successfully scheduled",
    "successfully scheduled",
    "appointment has been booked",
    "you're all set",
    "your appointment is confirmed",
    "booking confirmed",
]


def _looks_like_booking_confirmation(text: str) -> bool:
    lower = text.lower()
    return any(p in lower for p in _BOOKING_CONFIRMATION_PATTERNS)


# ── Auth dependency ──────────────────────────────────────────────────────


async def verify_vapi_secret(request: Request) -> None:
    """Validate the ``x-vapi-secret`` header against the stored secret."""
    expected = get_secrets().vapi_api_key
    if not expected:
        logger.error("Vapi secret not configured (VAPI_SECRET_ARN empty or unresolvable)")
        raise HTTPException(status_code=401, detail="Unauthorized")

    provided = request.headers.get("x-vapi-secret", "")
    if not provided or provided != expected:
        logger.warning(
            "Vapi secret mismatch: provided=%s expected=%s",
            provided[:8] + "..." if provided else "(empty)",
            expected[:8] + "..." if expected else "(empty)",
        )
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

    # Log payload structure for debugging
    msg = body.get("message")
    has_message = isinstance(msg, dict)
    event_type = msg.get("type", "") if has_message else ""
    logger.info(
        "Vapi webhook payload: keys=%s message_type=%s event=%s has_secret=%s",
        list(body.keys()),
        type(msg).__name__,
        event_type or "(none)",
        bool(request.headers.get("x-vapi-secret")),
    )

    # Server URL event: nested "message" dict with "type"
    if has_message and event_type:
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


# ── Office hours helper ──────────────────────────────────────────────────


def _build_office_hours_context(office_hours: list[dict], timezone: str) -> dict:
    """Build office hours context for the assistant prompt.

    Returns a dict with ``is_open``, ``prompt_snippet`` (text to inject into
    the system prompt), and the raw ``check_office_hours`` result.
    """
    info = check_office_hours(office_hours, timezone)
    is_open = info["is_open"]

    if is_open:
        snippet = ""
    else:
        parts = [f"The office is currently CLOSED (timezone: {timezone})."]
        if info["today_hours"]:
            parts.append(f"Today's hours: {info['today_hours']['start']} – {info['today_hours']['end']}.")
        if info["next_open"]:
            parts.append(f"Next open: {info['next_open']}.")
        parts.append(
            "If the caller asks to speak to someone or transfer, say: "
            "'Our office is currently closed. We're open again {next_open}. "
            "Is there anything else I can help you with?' "
            "Do NOT attempt the transfer.".format(next_open=info["next_open"] or "during business hours")
        )
        snippet = " ".join(parts)

    return {"is_open": is_open, "prompt_snippet": snippet, **info}


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
    to_phone, tenant_support_number = _resolve_to_phone_and_support(call_data)

    # Debug: log keys Vapi sends so we can see phoneNumber / assistantId presence
    logger.info(
        "Vapi assistant-request: call_id=%s from=***%s to=***%s assistantId=%s phoneNumber=%s",
        call_id,
        from_phone[-4:] if from_phone else "none",
        to_phone[-4:] if to_phone else "none",
        call_data.get("assistantId", "none"),
        call_data.get("phoneNumber", "none"),
    )

    webhook_secret = get_secrets().vapi_api_key
    session_key = f"vapi-{call_id}"

    # Authenticate caller to get their name
    is_store_caller = False
    first_name = ""
    client_name = "ProjectsForce"
    support_number = tenant_support_number  # Pre-populated from vapi-assistants table
    office_hours: list[dict] = []
    tenant_timezone = "US/Eastern"
    if from_phone:
        try:
            creds = await get_or_authenticate(from_phone, to_phone)
            user_name = creds.get("user_name", "")
            first_name = user_name.split()[0] if user_name and user_name.strip() else ""
            client_name = creds.get("client_name", "ProjectsForce") or "ProjectsForce"
            support_number = creds.get("support_number", "") or support_number
            office_hours = creds.get("office_hours", [])
            tenant_timezone = creds.get("timezone", "US/Eastern")
        except AuthenticationError as exc:
            # Auth failure = unknown caller (could be store/retailer or unrecognized customer)
            logger.info("Unknown caller detected (call_id=%s)", call_id)
            is_store_caller = True
            # PF API may return client_name and support_number even on auth failure
            if exc.client_name:
                client_name = exc.client_name
            if exc.support_number:
                support_number = exc.support_number
        except Exception:
            logger.exception("Phone auth failed during assistant-request (call_id=%s)", call_id)

    # Check office hours for transfer gating
    hours_context = _build_office_hours_context(office_hours, tenant_timezone)

    if is_store_caller:
        _store_sessions[session_key] = {"to_phone": to_phone, "authenticated": False, "support_number": support_number}
        greeting = _generate_store_greeting(client_name)
        logger.info("Vapi unknown caller greeting: call_id=%s client=%s office_open=%s", call_id, client_name, hours_context.get("is_open"))
        return {"assistant": _build_store_assistant_config(
            greeting, webhook_secret, client_name, support_number, hours_context,
        )}

    greeting = _generate_dynamic_greeting(first_name, client_name)
    logger.info(
        "Vapi dynamic greeting: call_id=%s name=%s client=%s office_open=%s",
        call_id,
        first_name or "(anonymous)",
        client_name,
        hours_context.get("is_open"),
    )

    return {"assistant": _build_assistant_config(greeting, webhook_secret, support_number, client_name, hours_context)}


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
    """Build a Vapi ``transferCall`` tool using blind transfer (SIP REFER).

    Blind transfer is the most reliable mode — Vapi initiates a standard SIP
    transfer to the support number, the customer hears ringing, and once the
    agent answers they are connected directly.  Vapi exits completely after
    the transfer.

    Returns a list with one tool dict if ``support_number`` is provided,
    or an empty list (so it can be unpacked with ``*`` into the tools array).
    """
    if not support_number:
        return []

    e164 = _normalize_e164(support_number)

    return [
        {
            "type": "transferCall",
            "messages": [
                {
                    "type": "request-start",
                    "content": (
                        "I'm transferring you now. "
                        "You'll hear ringing while I connect you."
                    ),
                },
                {
                    "type": "request-complete",
                    "content": "You're now connected. Have a great day!",
                },
                {
                    "type": "request-failed",
                    "content": (
                        "I wasn't able to connect you to our support team. "
                        f"You can reach them directly at {_format_phone_for_speech(e164)}. "
                        "Is there anything else I can help you with?"
                    ),
                },
            ],
            "destinations": [
                {
                    "type": "number",
                    "number": e164,
                    "transferPlan": {
                        "mode": "blind-transfer",
                    },
                }
            ],
        }
    ]


def _format_phone_for_speech(phone: str) -> str:
    """Format a phone number for TTS — reads as individual digits with pauses.

    Example: +19566699322 → '9. 5. 6. 6. 6. 9. 9. 3. 2. 2'
    """
    digits = "".join(ch for ch in phone if ch.isdigit())
    # Drop leading country code '1' for US numbers
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    # Group as (XXX) XXX-XXXX for natural reading
    if len(digits) == 10:
        return f"{digits[0:3]}, {digits[3:6]}, {digits[6:10]}"
    return ", ".join(digits)


def _build_assistant_config(
    first_message: str,
    server_secret: str = "",
    support_number: str = "",
    client_name: str = "ProjectsForce",
    hours_context: dict | None = None,
) -> dict:
    """Build the full Vapi assistant config returned on ``assistant-request``.

    This mirrors the assistant settings previously stored in Vapi's dashboard
    but with a dynamic ``firstMessage``.
    """
    name = client_name or "ProjectsForce"
    server_config: dict = {
        "url": _get_webhook_url(),
        "timeoutSeconds": 30,
    }
    if server_secret:
        server_config["secret"] = server_secret
    return {
        "name": f"{name} Scheduling Bot",
        "voice": _VOICE_CONFIG,
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
                        "6. If the user asks to speak to a person, transfer the call, or wants human support: "
                        "use the transferCall tool if available. Do NOT use filler phrases — just say "
                        "'I\\'m transferring you now' and invoke the transfer immediately. "
                        "If transferCall is NOT available, say: "
                        "'I\\'m sorry, I\\'m unable to transfer you right now. "
                        "Please contact our support team directly for further assistance.'\n"
                        "6b. Whenever you would read out a phone number, offer to transfer "
                        "the caller instead: 'Would you like me to connect you directly?' "
                        "If yes, use the transferCall tool. If transferCall is not available "
                        "or the caller declines, read the number.\n"
                        "7. Only handle basic greetings and goodbyes yourself. "
                        "Everything else goes through ask_scheduling_bot.\n"
                        "8. Keep your spoken responses concise — this is a phone call, not a text chat. "
                        "No bullet points, no markdown. "
                        "NEVER read out project numbers or IDs — they are long and unintelligible. "
                        "Instead, identify projects by their category/type and status.\n"
                        "9. Before calling ask_scheduling_bot, say a brief natural filler. "
                        "Vary your phrasing — rotate between: "
                        '"Give me one moment while I check that for you", '
                        '"Let me pull that up", "One second", '
                        '"Let me take a look". '
                        "Do NOT repeat the same phrase back to back. "
                        'NEVER say "Hold on", "Wait", or "Hang on" — these sound rude.\n'
                        "10. If the tool call fails, say: "
                        '"I\'m having trouble looking that up. Let me try again." and retry once.\n'
                        "11. CRITICAL — YOU CANNOT BOOK APPOINTMENTS YOURSELF. EVERY scheduling step "
                        "MUST go through ask_scheduling_bot. This includes:\n"
                        "   - Getting available dates → call ask_scheduling_bot\n"
                        "   - User picks a date/time → call ask_scheduling_bot with their choice\n"
                        "   - User confirms 'yes, book it' → call ask_scheduling_bot with 'yes'\n"
                        "   The appointment is NOT booked until ask_scheduling_bot returns a message "
                        "containing 'confirmed' or 'scheduled'. If you haven't received that, "
                        "the booking DID NOT HAPPEN. Never tell the user their appointment is "
                        "confirmed unless ask_scheduling_bot explicitly said so.\n"
                        "12. Do NOT end the call until ask_scheduling_bot has returned a confirmation "
                        "message OR the user explicitly says goodbye/bye/that's all I need. "
                        "If the user says 'yes' or 'go ahead' to book, you MUST call "
                        "ask_scheduling_bot ONE MORE TIME with their confirmation before ending."
                        + (
                            f"\n\nOFFICE HOURS: {hours_context['prompt_snippet']}"
                            if hours_context and hours_context.get("prompt_snippet")
                            else ""
                        )
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
        "serverUrl": _get_webhook_url(),
    }


def _generate_store_greeting(client_name: str = "ProjectsForce") -> str:
    """Build a generic SSML greeting for unknown callers.

    Does NOT assume the caller is a retailer — asks how we can help first,
    then qualifies them during the conversation.
    """
    name = client_name or "ProjectsForce"
    return (
        f'<break time="3000ms"/> Hello! I\'m J from {name}. '
        '<break time="300ms"/> '
        "How can I help you today?"
    )


def _build_store_assistant_config(
    first_message: str,
    server_secret: str = "",
    client_name: str = "ProjectsForce",
    support_number: str = "",
    hours_context: dict | None = None,
) -> dict:
    """Build the Vapi assistant config for unknown callers.

    Unknown callers go through a qualification flow:
    1. Generic greeting — "How can I help you?"
    2. If project-related — "Are you the customer or calling from a retailer?"
    3. Retailer — ask for project/PO number, restrict to status only
    4. Customer (unrecognized number) — offer to transfer to office
    5. Non-project call — transfer to office

    Uses ``ask_store_bot`` tool for retailer queries and ``transferCall``
    for office transfers when a support number is available.
    """
    name = client_name or "ProjectsForce"
    server_config: dict = {
        "url": _get_webhook_url(),
        "timeoutSeconds": 30,
    }
    if server_secret:
        server_config["secret"] = server_secret
    return {
        "name": f"{name} Assistant",
        "voice": _VOICE_CONFIG,
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
                        "You do NOT know who this caller is. Do NOT assume they are a "
                        "retailer or a customer. You must qualify them first.\n\n"
                        "## QUALIFICATION FLOW\n"
                        "1. You already greeted them. Wait for them to state their need.\n"
                        "2. If their request is about a project, order, installation, "
                        "or scheduling, ask: 'Are you the customer, or are you calling "
                        "from a retailer?'\n"
                        "   - Recognize these as RETAILER: store, retailer, Lowe's, "
                        "Home Depot, vendor, supplier, dealer, shop.\n"
                        "   - Recognize these as CUSTOMER: customer, homeowner, "
                        "I'm the customer, it's my project, I placed the order.\n"
                        "3. If RETAILER: ask for a project number or PO number "
                        "(do NOT ask for a customer name). Then call ask_store_bot "
                        "with lookup_type and lookup_value.\n"
                        "4. If CUSTOMER: say 'I don't recognize your phone number. "
                        "Let me transfer you to our team so they can help you.' "
                        "Then use the transferCall tool if available. If transferCall "
                        "is not available, say: 'Please contact our support team "
                        "directly and they\\'ll be happy to assist you.'\n"
                        "5. If their request is NOT project-related (e.g., job inquiry, "
                        "sales call, wrong number), say: 'Let me connect you with "
                        "someone who can help.' Then use the transferCall tool if available. "
                        "If not, say: 'Please contact our support team directly.'\n\n"
                        "## AFTER RETAILER AUTHENTICATION\n"
                        "Once authenticated via ask_store_bot, follow these rules:\n"
                        "- Use ask_store_bot for ALL subsequent queries. "
                        "You do NOT need lookup_type/lookup_value again.\n"
                        "- Pass the user's EXACT words in the \"question\" field. "
                        "Do NOT rephrase.\n"
                        "- NEVER share customer names, phone numbers, "
                        "email addresses, or street addresses. "
                        "Only share project status, scheduled dates, technician names, "
                        "project numbers, and PO numbers.\n"
                        "- NEVER offer to schedule, reschedule, or cancel appointments. "
                        "Retailer callers can ONLY check project status. If they ask, "
                        "say: 'Scheduling is not available for retailer calls. "
                        "Please have the customer call us directly.'\n"
                        "- NEVER read out project numbers or IDs — they are long and "
                        "unintelligible. Identify projects by category/type and status.\n\n"
                        "## GENERAL RULES\n"
                        "- Keep responses concise — no bullet points, no markdown.\n"
                        "- Before calling a tool, say a brief natural filler. "
                        "Vary your phrasing — rotate between: "
                        '"Give me one moment while I check that", '
                        '"Let me pull that up", "One second", '
                        '"Let me take a look". '
                        "Do NOT repeat the same phrase back to back. "
                        'NEVER say "Hold on", "Wait", or "Hang on" — these sound rude.\n'
                        "- Do NOT use filler phrases for transfers — just say "
                        "'I'm transferring you now' and invoke the transfer.\n"
                        "- Whenever you would read out a phone number, offer to transfer "
                        "the caller instead: 'Would you like me to connect you directly?' "
                        "If yes, use the transferCall tool. If transferCall is not available "
                        "or the caller declines, read the number.\n"
                        "- If transferCall is not available and you cannot transfer, say: "
                        "'Please contact our support team directly for further assistance.'\n"
                        "- NEVER ask clarifying questions before calling the tool. "
                        "Let the scheduling bot handle clarification."
                        + (
                            f"\n\nOFFICE HOURS: {hours_context['prompt_snippet']}"
                            if hours_context and hours_context.get("prompt_snippet")
                            else ""
                        )
                    ),
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "ask_store_bot",
                        "description": (
                            "Use for retailer callers ONLY. On first call, include "
                            "lookup_type and lookup_value to authenticate. After that, "
                            "only question is needed."
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
                                    ],
                                    "description": (
                                        "Type of lookup value. Required on first call."
                                    ),
                                },
                                "lookup_value": {
                                    "type": "string",
                                    "description": (
                                        "The project number or PO number. "
                                        "Required on first call."
                                    ),
                                },
                            },
                        },
                    },
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
        "serverUrl": _get_webhook_url(),
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
            # Store call — use /project-notes/add-note endpoint
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
            reset_confirm_flag()
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

            # Guardrail: detect hallucinated booking confirmations.
            # If the scheduling agent says "confirmed" without calling
            # confirm_appointment, retry with an explicit instruction.
            if not was_confirm_called() and _looks_like_booking_confirmation(voice_text):
                logger.warning(
                    "Hallucinated booking in Vapi call (call_id=%s) — retrying",
                    call_id,
                )
                reset_confirm_flag()
                response = await orchestrator.route_request(
                    user_input=(
                        "The customer confirmed. You MUST call the confirm_appointment "
                        "tool NOW to actually book the appointment. Do NOT respond "
                        "without calling the tool."
                    ),
                    user_id=user_id,
                    session_id=session_id,
                    additional_params={"channel": "vapi"},
                )
                retry_text = extract_response_text(response.output)
                if was_confirm_called():
                    voice_text = format_for_voice(retry_text)
                    logger.info("Vapi retry succeeded — confirm_appointment called")
                else:
                    logger.warning("Vapi retry also failed to call confirm_appointment")
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
                "I need a project number or PO number to look that up. "
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
            support = store_session.get("support_number", "")
            msg = (
                "I couldn't find an account with that information. "
                "Could you double-check and try again?"
            )
            if support:
                msg += (
                    f" Or if you'd prefer, I can transfer you to our support team "
                    f"— or give you the number: {support}."
                )
            return _build_tool_result(msg, tool_call_id)
        store_session["creds"] = creds
        store_session["authenticated"] = True
        store_session["support_number"] = creds.get("support_number", "")
        store_session["client_name"] = creds.get("client_name", "")
        _store_sessions[session_key] = store_session

    if not question:
        msg = (
            "You're verified! How can I help you? "
            "I can look up project status and details."
        )
        support = store_session.get("support_number", "")
        if support:
            msg += (
                f" If you need to speak with someone, "
                f"the support number is {support}."
            )
        return _build_tool_result(msg, tool_call_id)

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
        "[STORE CALLER — status and technician names only, no scheduling, no customer PII] "
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
            timezone=creds.get("timezone", "US/Eastern"),
            support_number=creds.get("support_number", ""),
            support_email=creds.get("support_email", ""),
            office_hours=creds.get("office_hours", []),
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
            timezone=creds.get("timezone", "US/Eastern"),
            support_number=creds.get("support_number", ""),
            support_email=creds.get("support_email", ""),
            office_hours=creds.get("office_hours", []),
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
    """Resolve the destination phone number from call data."""
    phone, _ = _resolve_to_phone_and_support(call_data)
    return phone


def _resolve_to_phone_and_support(call_data: dict) -> tuple[str, str]:
    """Resolve destination phone and support number from call data.

    Returns (to_phone, support_number). Support number comes from the
    vapi-assistants table (registered per tenant).
    """
    support_number = ""

    # 1. Direct from call data (Twilio/Vonage-backed Vapi numbers)
    phone_obj = call_data.get("phoneNumber")
    if isinstance(phone_obj, dict):
        phone = phone_obj.get("number", "")
        if phone:
            return phone, support_number
    elif isinstance(phone_obj, str) and phone_obj:
        return phone_obj, support_number

    # 2. Look up by assistant ID (multi-tenant, Vapi-managed numbers)
    assistant_id = call_data.get("assistantId", "")
    if assistant_id:
        info = get_assistant_info(assistant_id)
        phone = info.get("phone_number", "")
        support_number = info.get("support_number", "")
        if phone:
            return phone, support_number

    # 3. Legacy env var fallback
    settings = get_settings()
    return settings.vapi_phone_number, support_number or settings.default_support_number


def _build_tool_result(text: str, tool_call_id: str = "") -> dict:
    """Build a Vapi tool-result response with single-line text."""
    single_line = re.sub(r"\s*\n\s*", " ", text).strip()
    result_entry: dict = {"result": single_line}
    if tool_call_id:
        result_entry["toolCallId"] = tool_call_id
    return {"results": [result_entry]}
