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
from datetime import datetime
from functools import lru_cache

import boto3
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
from channels.outbound_consumer import retry_outbound_call
from channels.outbound_store import (
    cache_active_call,
    get_active_call,
    get_outbound_call,
    remove_active_call,
    update_outbound_call,
)
from channels.vapi_config import get_assistant_info, get_phone_for_assistant
from config import get_secrets, get_settings
from observability.logging import RequestContext
from orchestrator import get_orchestrator
from orchestrator.response_utils import extract_response_text
from tools.pii_filter import scrub_pii
from tools.scheduling import add_note as sched_add_note
from tools.scheduling import (
    _reschedule_pending,
    cleanup_call_caches,
    clear_reschedule_old_appointment,
    clear_session_projects,
    get_last_project_id,
    get_reschedule_old_appointment,
    get_session_notes,
    get_session_projects,
    post_call_summary_notes,
    post_store_call_notes,
    reset_action_flags,
    reset_confirm_flag,
    reset_request_caches,
    session_action_completed,
    session_has_any_completed,
    was_address_updated,
    was_cancel_called,
    was_confirm_called,
    was_note_added,
    was_time_slots_called,
)
from tools.scheduling import confirm_appointment as sched_confirm_appointment
from tools.scheduling import get_available_dates as sched_get_available_dates
from tools.scheduling import get_time_slots as sched_get_time_slots

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


# Environment-aware base URLs (without path suffix)
_BASE_URLS = {
    "dev": "https://schedulingagent.dev.projectsforce.com",
    "qa": "https://schedulingagent.qa.projectsforce.com",
    "staging": "https://schedulingagent.staging.projectsforce.com",
    "prod": "https://schedulingagent.apps.projectsforce.com",
}


def _get_base_url() -> str:
    env = get_settings().environment
    return _BASE_URLS.get(env, _BASE_URLS["dev"])


# Shared voice config — used by main assistant, store assistant, AND transfer assistant
# so the caller hears a consistent voice throughout the entire call.
_VOICE_CONFIG = {
    "provider": "cartesia",
    "model": "sonic-3",
    "voiceId": "e07c00bc-4134-4eae-9ea4-1a55fb45746b",
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

# Call auth cache: call_id → creds dict.
# Populated during assistant-request (after phone auth), consumed by the
# Custom LLM endpoint (POST /vapi/chat/completions) so it can set AuthContext
# without re-authenticating on every request.
_call_auth_cache: dict[str, dict] = {}

# Call-level project pinning: call_id → project_id.
# Once the scheduling agent starts working on a project during a call,
# that project_id is locked for the entire call.  Prevents GPT-4o-mini
# from drifting to a different project (especially during guardrail retries).
_call_project_pin: dict[str, str] = {}


def get_call_auth(call_id: str) -> dict | None:
    """Retrieve cached auth credentials for a call."""
    return _call_auth_cache.get(call_id)


def remove_call_auth(call_id: str) -> None:
    """Remove cached auth credentials when a call ends."""
    _call_auth_cache.pop(call_id, None)

# ---------------------------------------------------------------------------
#  Intent-based guardrail classifier
#
#  Replaces fragile pattern lists with a single LLM call that detects
#  whether the response *claims* a write action was completed.  Only
#  invoked when at least one write-action ContextVar flag is False.
# ---------------------------------------------------------------------------

_GUARDRAIL_CLASSIFIER_PROMPT = """\
You are a guardrail classifier for a phone scheduling assistant.

Analyze the assistant's response and determine if it CLAIMS any of these \
actions were already completed:

- **confirm**: The assistant says an appointment/installation was booked, \
confirmed, or scheduled.
- **cancel**: The assistant says an appointment was cancelled or removed.
- **note**: The assistant says a note was added, saved, recorded, or noted.
- **address**: The assistant says an address was updated, saved, noted, or \
will be reviewed.

Return ONLY a JSON array of the claimed action names. If none are claimed, \
return an empty array.

Examples:
- "Your appointment is confirmed for Tuesday at 9 AM." → ["confirm"]
- "I've added that note about the big dog." → ["note"]
- "The appointment has been cancelled and I've noted the reason." → ["cancel", "note"]
- "Here are your available dates: April 28, 29, 30." → []
- "Would you like me to schedule that for you?" → []
- "I'll add that note for you right away." → ["note"]
"""


@lru_cache(maxsize=1)
def _get_guardrail_bedrock_client():
    """Bedrock client for guardrail classifier (cached singleton)."""
    settings = get_settings()
    return boto3.client("bedrock-runtime", region_name=settings.aws_region)


def _classify_claimed_actions(text: str) -> set[str]:
    """Use LLM to detect which write actions the response claims were completed.

    Returns a set of claimed action names: {"confirm", "cancel", "note", "address"}.
    Falls back to empty set on any error (fail-open — no false guardrail triggers).
    """
    if not text or len(text) < 10:
        return set()

    result_text = ""
    try:
        client = _get_guardrail_bedrock_client()
        settings = get_settings()
        response = client.converse(
            modelId=settings.bedrock_model_id,
            messages=[{"role": "user", "content": [{"text": text}]}],
            system=[{"text": _GUARDRAIL_CLASSIFIER_PROMPT}],
            inferenceConfig={"maxTokens": 50, "temperature": 0.0},
        )
        result_text = response["output"]["message"]["content"][0]["text"].strip()
        # Extract JSON array — LLM sometimes adds explanation text after the array
        match = re.search(r"\[.*?\]", result_text, re.DOTALL)
        if match:
            claimed = json.loads(match.group())
            if isinstance(claimed, list):
                valid = {"confirm", "cancel", "note", "address"}
                return {a for a in claimed if a in valid}
    except Exception:
        logger.exception("Guardrail classifier failed (raw=%s) — skipping", result_text or "N/A")
    return set()


async def _classify_claimed_actions_async(text: str) -> set[str]:
    """Async wrapper — runs the sync Bedrock call in a thread."""
    return await asyncio.to_thread(_classify_claimed_actions, text)


# Regex to detect fabricated time slots: 3+ AM/PM time patterns in a response
# indicates the LLM is presenting time slots (which must come from get_time_slots).
# Kept as pattern-based detection — this checks data structure, not intent.
_TIME_SLOT_PATTERN = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)\b")

_TIME_SLOT_CONTEXT_PHRASES = [
    "available time", "time slot", "choose a time", "pick a time",
    "select a time", "available slot", "open slot", "schedule for",
    "available for scheduling", "here are the time", "following time",
]


def _looks_like_time_slot_list(text: str) -> bool:
    """Detect if the response contains a fabricated list of time slots.

    Requires BOTH 3+ AM/PM times AND scheduling context phrases to avoid
    false positives on project data that contains scheduled times.
    """
    matches = _TIME_SLOT_PATTERN.findall(text)
    if len(matches) < 3:
        return False
    lower = text.lower()
    return any(phrase in lower for phrase in _TIME_SLOT_CONTEXT_PHRASES)


# Pattern to strip fabricated time slot sentences (e.g., "Available times are 8 AM, 9 AM, ...")
_TIME_SLOT_SENTENCE = re.compile(
    r"[^.!?\n]*\b\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)\b[^.!?\n]*(?:[.!?]|\n|$)",
    re.IGNORECASE,
)


def _strip_time_slots(text: str) -> str:
    """Remove sentences containing time-slot patterns from a response.

    Used as a last-resort fallback when the LLM fabricates time slots
    and the retry also fails.
    """
    cleaned = _TIME_SLOT_SENTENCE.sub("", text).strip()
    # If stripping removed everything, keep the original minus the slots list
    if not cleaned:
        cleaned = _TIME_SLOT_PATTERN.sub("", text).strip()
    # Collapse multiple spaces / newlines
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"  +", " ", cleaned)
    return cleaned.strip()


# Pattern to detect if a user utterance is a date selection (e.g., "April 29", "the 23rd", "next Monday")
_DATE_SELECTION_PATTERN = re.compile(
    r"(?:"
    r"(?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+\d{1,2}"                       # "April 29"
    r"|"
    r"\d{1,2}(?:st|nd|rd|th)"           # "29th", "23rd"
    r"|"
    r"(?:next\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"  # "next Monday"
    r"|"
    r"\d{1,2}[/-]\d{1,2}"              # "4/29", "04-29"
    r")",
    re.IGNORECASE,
)


def _looks_like_date_selection(text: str) -> bool:
    """Check if the user's question looks like they're picking a specific date."""
    return bool(_DATE_SELECTION_PATTERN.search(text))


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
        return await _handle_server_event(body)

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

    # ── Outbound call detection ──────────────────────────────────────
    call_type = call_data.get("type", "")
    our_call_id = (call_data.get("metadata") or {}).get("call_id", "")
    if call_type == "outboundPhoneCall" and our_call_id:
        return await _handle_outbound_assistant_request(body, call_data, our_call_id)

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
        if not support_number:
            logger.warning(
                "No support_number for store caller — transfer disabled: "
                "call_id=%s client=%s to_phone=***%s",
                call_id, client_name, to_phone[-4:] if to_phone else "none",
            )
        _store_sessions[session_key] = {"to_phone": to_phone, "authenticated": False, "support_number": support_number}
        greeting = _generate_store_greeting(client_name)
        logger.info("Vapi unknown caller greeting: call_id=%s client=%s office_open=%s has_transfer=%s", call_id, client_name, hours_context.get("is_open"), bool(support_number))
        return {"assistant": _build_store_assistant_config(
            greeting, webhook_secret, client_name, support_number, hours_context,
        )}

    # Cache auth creds for Custom LLM endpoint (keyed by call_id)
    if creds:
        _call_auth_cache[call_id] = creds

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
        "timeoutSeconds": 60,
    }
    if server_secret:
        server_config["secret"] = server_secret
    return {
        "name": f"{name} Scheduling Bot",
        "voice": _VOICE_CONFIG,
        "model": {
            "model": "gpt-4o",
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
                        "9. FILLER RULES: Say 'One moment.' ONLY when the user asks a NEW question "
                        "that requires a tool call (e.g. 'what are my projects?', 'what dates are available?'). "
                        "Do NOT say any filler when the user is just replying to your question "
                        "(e.g. picking a date, saying 'yes', choosing a time slot). "
                        "NEVER say 'Hold on', 'Wait', 'Hang on', 'Just a sec', 'Give me a moment', "
                        "'Let me check', 'Let me pull that up', or 'One second'. "
                        "The ONLY allowed filler is 'One moment.' — nothing else, and only once per tool call.\n"
                        "10. If the tool call fails or times out, say: "
                        '"Let me try that again." and retry once. '
                        "NEVER say 'I'm having trouble looking that up' or fabricate an error "
                        "message. Only report a problem if the tool fails TWICE.\n"
                        "11. CRITICAL — YOU CANNOT BOOK, CANCEL, OR RESCHEDULE APPOINTMENTS YOURSELF. "
                        "EVERY scheduling step MUST go through ask_scheduling_bot. This includes:\n"
                        "   - Getting available dates → call ask_scheduling_bot\n"
                        "   - User picks a date/time → call ask_scheduling_bot with their choice\n"
                        "   - User confirms 'yes, book it' → call ask_scheduling_bot with 'yes'\n"
                        "   - User wants to cancel → call ask_scheduling_bot with their cancel request\n"
                        "   - User wants to reschedule → call ask_scheduling_bot with their reschedule request\n"
                        "   The action is NOT done until ask_scheduling_bot returns a SUCCESS message. "
                        "NEVER say 'your appointment is booked/cancelled/rescheduled' unless "
                        "ask_scheduling_bot explicitly said so in its response. "
                        "If you haven't received confirmation from the tool, THE ACTION DID NOT HAPPEN.\n"
                        "12. Do NOT end the call until ask_scheduling_bot has returned a confirmation "
                        "message OR the user explicitly says goodbye/bye/that's all I need. "
                        "If the user says 'yes' or 'go ahead' to book, you MUST call "
                        "ask_scheduling_bot ONE MORE TIME with their confirmation before ending.\n"
                        "13. NEVER add your own information to tool calls. You do NOT know the "
                        "customer's projects, dates, time slots, or appointment details. "
                        "ONLY the scheduling bot knows this. Do NOT include dates, times, "
                        "slot lists, or project details in the question — just pass "
                        "the user's exact words. The scheduling bot has all the context."
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
                                        "The user's EXACT words — pass VERBATIM what they said. "
                                        "Do NOT rephrase, summarize, or add ANY context. "
                                        "Do NOT include dates, times, project details, or "
                                        "time slots — the bot already knows everything. "
                                        "Example: user says 'April 22nd' → pass 'April 22nd'"
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


def _build_custom_llm_assistant_config(
    first_message: str,
    server_secret: str = "",
    support_number: str = "",
    client_name: str = "ProjectsForce",
    hours_context: dict | None = None,
) -> dict:
    """Build Vapi assistant config using Custom LLM — our Claude handles all reasoning.

    Instead of GPT-4o-mini calling ``ask_scheduling_bot``, Vapi sends each
    user utterance directly to ``POST /vapi/chat/completions`` where our
    Bedrock Claude + AgentSquad orchestrator handles everything.

    The ``model.tools`` only includes ``transferCall`` (Vapi-native); all
    scheduling tools are handled server-side by the orchestrator.
    """
    name = client_name or "ProjectsForce"
    server_config: dict = {
        "url": _get_webhook_url(),
        "timeoutSeconds": 60,
    }
    if server_secret:
        server_config["secret"] = server_secret

    # Minimal system prompt — voice style + office hours only.
    # All reasoning is done by our Claude via the Custom LLM endpoint.
    system_content = (
        f"You are J, a friendly phone assistant for {name} "
        "— a home improvement scheduling service.\n"
        "Keep your responses concise and conversational — this is a phone call.\n"
        "No bullet points, no markdown.\n"
        "NEVER read out project numbers or IDs — they are long and unintelligible.\n"
        "Identify projects by their category/type and status."
    )
    if hours_context and hours_context.get("prompt_snippet"):
        system_content += f"\n\nOFFICE HOURS: {hours_context['prompt_snippet']}"

    return {
        "name": f"{name} Scheduling Bot",
        "voice": _VOICE_CONFIG,
        "model": {
            "provider": "custom-llm",
            "model": "scheduling-agent",
            "url": f"{_get_base_url()}/vapi/chat/completions",
            "metadataSendMode": "variable",
            "messages": [
                {"role": "system", "content": system_content},
            ],
            "tools": [
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


# ── Outbound call handling ────────────────────────────────────────────


async def _handle_outbound_assistant_request(
    body: dict, call_data: dict, our_call_id: str
) -> dict:
    """Handle assistant-request for an outbound call.

    Looks up the call from the active calls cache (or DynamoDB fallback),
    builds an outbound-specific assistant config, and returns it.
    """
    vapi_call_id = call_data.get("id", "")
    logger.info(
        "Outbound assistant-request: vapi_call_id=%s our_call_id=%s",
        vapi_call_id, our_call_id,
    )

    # Look up call context (cache first, then DynamoDB)
    outbound = get_active_call(vapi_call_id)
    if not outbound:
        outbound = await get_outbound_call(our_call_id)
    if not outbound:
        logger.error("Outbound call not found: %s", our_call_id)
        # Return a minimal config that tells the AI to apologize
        return {"assistant": _build_assistant_config(
            "I'm sorry, I'm experiencing a technical issue. Please try again later.",
            get_secrets().vapi_api_key,
        )}

    # Update status to in_progress
    await update_outbound_call(our_call_id, {"status": "in_progress"})

    webhook_secret = get_secrets().vapi_api_key
    customer_name = outbound.get("customer_name", "")
    client_name = outbound.get("client_name", "ProjectsForce")
    support_number = (outbound.get("auth_creds") or {}).get("support_number", "")

    # Use actual project type from prefetched data (e.g., "Flooring Installation")
    prefetched_project = (outbound.get("prefetched") or {}).get("project", {})
    project_type = (
        prefetched_project.get("projectType", "")
        or prefetched_project.get("category", "")
        or outbound.get("project_type", "")
    )

    # Office hours
    office_hours = (outbound.get("auth_creds") or {}).get("office_hours", [])
    timezone = (outbound.get("auth_creds") or {}).get("timezone", "US/Eastern")
    hours_context = _build_office_hours_context(office_hours, timezone)

    # Pin project for the entire outbound call (known from SQS message)
    project_id = outbound.get("project_id", "")
    if project_id and vapi_call_id:
        _call_project_pin[vapi_call_id] = project_id

    greeting = _generate_outbound_greeting(customer_name, client_name, project_type)
    logger.info(
        "Outbound config: call_id=%s customer=%s client=%s project_type=%s project_id=%s",
        our_call_id, customer_name, client_name, project_type, project_id,
    )

    return {"assistant": _build_outbound_scheduling_config(
        greeting, webhook_secret, outbound, support_number, client_name, hours_context,
    )}


def _generate_outbound_greeting(
    customer_name: str, client_name: str, project_type: str
) -> str:
    """Build an SSML greeting for outbound scheduling calls."""
    first_name = customer_name.split()[0] if customer_name and customer_name.strip() else ""
    name_part = f"Hello {first_name}!" if first_name else "Hello!"
    name = client_name or "ProjectsForce"

    project_part = ""
    if project_type:
        project_part = f" I'm calling about scheduling your {project_type.lower()}."
    else:
        project_part = " I'm calling about scheduling your upcoming project."

    return (
        f'<break time="2000ms"/> {name_part} <break time="300ms"/> '
        f"This is J from {name}."
        f' <break time="300ms"/> '
        f"{project_part}"
        ' <break time="500ms"/> '
        "Is now a good time to talk?"
    )


def _format_prefetched_dates(dates_data: dict) -> str:
    """Format pre-fetched date data as a concise summary with recommendation.

    Instead of listing every date, produces:
    - Date range (e.g. "April 8 through April 16")
    - Weather summary (e.g. "mostly good conditions")
    - Recommended date (best weather day)
    - Available time slots
    """
    from datetime import datetime as dt

    weather = dates_data.get("dates_with_weather", [])
    plain_dates = dates_data.get("available_dates", [])
    time_slots = dates_data.get("available_time_slots", [])

    if not plain_dates and not weather:
        return ""

    # Parse date range
    all_dates = plain_dates or [
        e[0] if isinstance(e, (list, tuple)) else e.get("date", "")
        for e in weather
    ]
    parsed_dates = []
    for d in all_dates:
        try:
            raw = d["date"] if isinstance(d, dict) else d
            parsed_dates.append(dt.strptime(raw, "%Y-%m-%d"))
        except (ValueError, TypeError):
            pass

    result = ""
    if parsed_dates:
        first = min(parsed_dates)
        last = max(parsed_dates)
        if first == last:
            result += f"**Date Range:** {first.strftime('%B %d')} (1 date available)\n"
        else:
            result += (
                f"**Date Range:** {first.strftime('%B %d')} through "
                f"{last.strftime('%B %d')} ({len(parsed_dates)} dates available)\n"
            )

    # Weather summary + recommendation
    if weather:
        good, moderate, other = 0, 0, 0
        best_date = None
        for entry in weather:
            if isinstance(entry, (list, tuple)) and len(entry) >= 5:
                date_str, day, condition, high, indicator = entry[:5]
                ind = str(indicator).upper()
                if "GOOD" in ind:
                    good += 1
                    if not best_date:
                        best_date = (date_str, day, condition, high)
                elif "MODERATE" in ind:
                    moderate += 1
                    if not best_date:
                        best_date = (date_str, day, condition, high)
                else:
                    other += 1

        total = good + moderate + other
        if good == total:
            result += "**Weather:** Good conditions throughout\n"
        elif good + moderate == total:
            result += "**Weather:** Mostly good to moderate conditions\n"
        elif good > 0:
            result += f"**Weather:** Mixed — {good} good days, {moderate} moderate\n"
        else:
            result += "**Weather:** Mostly moderate conditions\n"

        if best_date:
            d_str, day_name, cond, high = best_date
            try:
                rec = dt.strptime(d_str, "%Y-%m-%d")
                result += (
                    f"**Recommended Date:** {rec.strftime('%A, %B %d')} "
                    f"— {cond}, {high}°F\n"
                )
            except ValueError:
                result += f"**Recommended Date:** {day_name} {d_str} — {cond}, {high}°F\n"

    if time_slots:
        result += f"**Time Slots:** {', '.join(time_slots)}\n"

    # Also keep the full list for reference (so the AI can answer follow-ups)
    if weather:
        result += "\n<details for reference — do NOT read aloud>\n"
        for entry in weather:
            if isinstance(entry, (list, tuple)) and len(entry) >= 5:
                date_str, day, condition, high, indicator = entry[:5]
                result += f"  {day} {date_str}: {condition} {high}°F {indicator}\n"
        result += "</details>\n"

    return result


def _format_address_for_speech(address: dict) -> str:
    """Format address for natural TTS reading.

    Returns e.g. ``910 North Harbor Drive, San Diego, CA 92101``.
    Does NOT spell digits individually.
    """
    parts = []
    if address.get("address1"):
        parts.append(address["address1"])
    city_state = []
    if address.get("city"):
        city_state.append(address["city"])
    if address.get("state"):
        city_state.append(address["state"])
    if city_state:
        parts.append(", ".join(city_state))
    if address.get("zipcode"):
        parts.append(address["zipcode"])
    return ", ".join(parts)


def _outbound_scheduling_tools(
    support_number: str, client_name: str, has_dates: bool
) -> list[dict]:
    """Build the Vapi tool definitions for outbound scheduling calls.

    Direct tools — Vapi AI calls these directly.  Our webhook routes them
    to the scheduling functions without going through the orchestrator.
    The ``project_id`` is injected server-side from the active call cache.
    """
    tools: list[dict] = [
        {
            "type": "function",
            "function": {
                "name": "get_time_slots",
                "description": (
                    "Get available time slots for a specific date. "
                    "Call this when the customer picks a date."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["date"],
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": "The date in YYYY-MM-DD format (e.g. 2026-04-10)",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "confirm_appointment",
                "description": (
                    "Book the appointment. Call ONLY after the customer "
                    "has confirmed their preferred date and time."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["date", "time"],
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": "The appointment date in YYYY-MM-DD format",
                        },
                        "time": {
                            "type": "string",
                            "description": "The time slot (e.g. '8:00 AM - 10:00 AM')",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "add_note",
                "description": (
                    "Add a note to the project. For address update requests, "
                    "start with 'Customer requested installation address update. New address is'. "
                    "For customer notes, start with 'CUSTOMER NOTE:'."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["note_text"],
                    "properties": {
                        "note_text": {
                            "type": "string",
                            "description": (
                                "The note text. For address changes start with "
                                "'Customer requested installation address update. New address is'. "
                                "For general notes start with 'CUSTOMER NOTE:'."
                            ),
                        },
                    },
                },
            },
        },
    ]

    # Fallback: include get_available_dates when dates aren't pre-fetched
    if not has_dates:
        tools.append({
            "type": "function",
            "function": {
                "name": "get_available_dates",
                "description": "Fetch available scheduling dates with weather information.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        })

    tools.extend(_transfer_call_tool(support_number, client_name))
    return tools


def _build_outbound_scheduling_config(
    first_message: str,
    server_secret: str,
    outbound_call: dict,
    support_number: str = "",
    client_name: str = "ProjectsForce",
    hours_context: dict | None = None,
) -> dict:
    """Build Vapi assistant config for outbound scheduling calls.

    Uses the same structure as inbound _build_assistant_config() but with
    an outbound-specific system prompt encoding the 6-step call flow.
    If ``outbound_call["prefetched"]`` contains dates/address, they are
    injected into the prompt so the AI can present them immediately.
    """
    name = client_name or "ProjectsForce"
    customer_name = outbound_call.get("customer_name", "")
    first_name = customer_name.split()[0] if customer_name and customer_name.strip() else "the customer"

    # Resolve actual project type from prefetched data (e.g., "Flooring Installation")
    # rather than SQS tenant_info metadata (which gives "project update")
    prefetched = outbound_call.get("prefetched", {})
    prefetched_project = prefetched.get("project", {})
    project_type = (
        prefetched_project.get("projectType", "")
        or prefetched_project.get("category", "")
        or outbound_call.get("project_type", "")
    )

    support_speech = ""
    if support_number:
        support_speech = _format_phone_for_speech(support_number)

    server_config: dict = {
        "url": _get_webhook_url(),
        "timeoutSeconds": 60,
    }
    if server_secret:
        server_config["secret"] = server_secret

    # Check for pre-fetched data
    dates_data = prefetched.get("dates", {})
    has_dates = bool(dates_data.get("available_dates"))

    # Build the pre-loaded data section for the prompt
    preloaded_section = ""
    if has_dates:
        preloaded_section = "\n## PRE-LOADED PROJECT DATA (already fetched — DO NOT call any tool for this)\n"
        preloaded_section += (
            "IMPORTANT: This data is ALREADY loaded. DO NOT call get_available_dates "
            "or any other tool to look up dates. Present this data DIRECTLY.\n\n"
        )
        preloaded_section += _format_prefetched_dates(dates_data) + "\n"

    # Build Step 3 — different depending on whether we have pre-fetched dates
    if has_dates:
        step3 = (
            "### Step 3 — Scheduling\n"
            "CRITICAL: You ALREADY have the dates in PRE-LOADED DATA above. "
            "DO NOT call get_available_dates — it is NOT available as a tool. "
            "Present the dates IMMEDIATELY with NO delay and NO filler phrases.\n\n"
            "**How to present dates (MANDATORY format):**\n"
            "Say ONE sentence summarizing the range and recommend the best date. Example:\n"
            "  'We have dates available from Monday the twentieth through "
            "Friday the twenty-fifth. Weather looks good throughout. "
            "I'd recommend Tuesday the twenty-first — clear skies and seventy-one degrees. "
            "Would that work for you?'\n\n"
            "**Rules:**\n"
            "- NEVER list dates one by one. Only summarize the range.\n"
            "- ALWAYS recommend the best weather date.\n"
            "- Speak dates as words: 'the twenty-first', NOT '21st' or 'April 21'.\n"
            "- If they agree, call get_time_slots with that date (YYYY-MM-DD format).\n"
            "- If they want a different date, accommodate if it's in the range.\n"
            "- If they pick a date NOT in the list: "
            "'That date isn't available. The closest options are [X] and [Y].'\n"
            "- Read time slots briefly: 'I have [X] and [Y]. Which works better?'\n"
            "- Customer picks a time → confirm before booking:\n"
            "  'I'll schedule that for [day] the [date] at [time]. Sound good?'\n"
            "- Wait for YES before calling confirm_appointment.\n"
        )
    else:
        step3 = (
            "### Step 3 — Scheduling\n"
            f"- Say: 'Let me check the available dates for your {project_type or 'project'}.'\n"
            "- Call get_available_dates to fetch dates with weather information.\n"
            "- Present the dates as a summary with a recommendation (not one by one).\n"
            "- Customer picks a date → call get_time_slots with that date.\n"
            "- Read time slots, customer picks → SUMMARIZE before booking:\n"
            "  'Just to confirm — I'll schedule your appointment for [date] "
            "between [time slot]. Shall I go ahead and book that?'\n"
            "- Wait for the customer to say YES before calling confirm_appointment.\n"
            "- The appointment is NOT booked until confirm_appointment returns 'confirmed'.\n"
        )

    # Build Step 4+5 — Notes + Wrap-up (no address confirmation needed)
    step45 = (
        "### Step 4 — Additional Notes\n"
        "- Ask: 'Is there anything our team should know before arriving? "
        "For example, pets, gate codes, or parking instructions?'\n"
        "- If the customer has notes, call add_note with "
        "'CUSTOMER NOTE: [their notes]'\n"
        "- If they say no / nothing: move on.\n\n"
        "### Step 5 — Wrap-up\n"
        f"Say: 'You\\'ll receive a confirmation text and email shortly. "
        f"Thank you for choosing {name}! Have a great day.'\n"
        "End the call.\n"
    )

    system_prompt = (
        f"You are J, a friendly and concise phone assistant for {name}.\n\n"
        "## YOUR MISSION\n"
        f"Schedule {first_name}'s {project_type or 'upcoming'} project. "
        "Be natural, brief, and conversational — like a human scheduler.\n"
        + preloaded_section
        + "\n## CALL FLOW\n\n"
        "### Step 1 — Introduction (already done)\n"
        "Wait for the customer's response.\n\n"
        "### Step 2 — Availability Check\n"
        "If they say no or seem busy: 'No problem! You can call us back anytime"
        + (f" at {support_speech}" if support_speech else "")
        + ". Have a great day!' — then end the call.\n"
        "If yes, proceed.\n\n"
        + step3
        + "\n"
        + step45
        + "\n"
        "## STYLE RULES\n"
        "- Speak naturally and concisely, like a human scheduler.\n"
        "- Keep every response to 1-2 sentences MAX. Do not monologue.\n"
        "- Only discuss THIS project ("
        + (project_type or "the one you called about")
        + "). Do NOT mention other projects the customer may have.\n"
        "- Do NOT proactively ask for the installation address. But if the customer "
        "volunteers an address correction, capture it with add_note.\n"
        "- FILLER RULES: When a tool call is running, say 'One moment.' and NOTHING else. "
        "NEVER say 'Just a sec', 'Give me a moment', 'Hold on', 'Wait', "
        "'Hang on', 'Let me check', or any other filler. One filler per tool call MAX.\n"
        "- Speak dates as ordinal words: 'the twenty-first', NOT '21st'.\n"
        "- Call each tool ONCE per action. If the tool already returned "
        "a result, do NOT call it again for the same thing.\n\n"
        "## TOOL RULES\n"
        "1. get_time_slots: Call with a date (YYYY-MM-DD) to get available time slots.\n"
        "2. confirm_appointment: Call with date and time ONLY after you have "
        "summarized the appointment and the customer has said YES. "
        "NEVER call this without verbal confirmation first.\n"
        "3. add_note: For address changes, start note_text with 'Customer requested installation address update. New address is'. "
        "For customer notes (gate codes, pets, parking, etc.), start with 'CUSTOMER NOTE:'.\n"
        "4. NEVER read project numbers or IDs aloud to the customer.\n"
        "5. If the customer wants a person, use transferCall.\n"
        "6. If the customer asks about weather — you ALREADY have the weather data "
        "in PRE-LOADED DATA above. Answer directly, no tool call needed.\n"
        "7. Past dates are NOT allowed. Scheduling is only available from tomorrow onwards. "
        "If the customer suggests a past date, say: 'That date has already passed. "
        "Let me check the next available dates for you.'"
        + (
            f"\n\nOFFICE HOURS: {hours_context['prompt_snippet']}"
            if hours_context and hours_context.get("prompt_snippet")
            else ""
        )
    )

    voicemail_msg = (
        f"Hello {first_name}, this is J from {name}. "
        f"I'm calling about scheduling your {project_type.lower() if project_type else 'upcoming project'}. "
        "We'd like to find a convenient time for you. "
    )
    if support_speech:
        voicemail_msg += f"Please call us back at {support_speech}. "
    voicemail_msg += "Thank you!"

    return {
        "name": f"{name} Outbound Scheduling",
        "voice": _VOICE_CONFIG,
        "model": {
            "model": "gpt-4o",
            "provider": "openai",
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": system_prompt}
            ],
            "tools": _outbound_scheduling_tools(support_number, name, has_dates),
        },
        "transcriber": {
            "model": "nova-3",
            "language": "en",
            "provider": "deepgram",
            "endpointing": 150,
        },
        "firstMessage": first_message,
        "endCallMessage": "",
        "endCallPhrases": [
            "goodbye", "bye", "bye bye", "bye now",
            "talk to you later", "have a great day",
            "no thanks", "not interested",
        ],
        "endCallFunctionEnabled": True,
        "voicemailDetection": {
            "provider": "vapi",
            "voicemailDetectionTypes": ["machine_end_beep"],
        },
        "voicemailMessage": voicemail_msg,
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


def _classify_outbound_outcome(ended_reason: str, summary: str) -> dict:
    """Classify the outcome of an outbound call based on Vapi's end-of-call data."""
    reason_lower = (ended_reason or "").lower()
    summary_lower = (summary or "").lower()

    # Voicemail detection
    if "voicemail" in reason_lower or "machine" in reason_lower:
        return {"status": "voicemail", "ended_reason": ended_reason, "summary": summary}

    # No answer / busy
    if "no-answer" in reason_lower or "busy" in reason_lower:
        return {"status": "no_answer", "ended_reason": ended_reason, "summary": summary}

    # Customer requested callback
    if any(phrase in summary_lower for phrase in ("call back", "callback", "not a good time", "busy right now")):
        return {"status": "callback_requested", "ended_reason": ended_reason, "summary": summary}

    # Successful completion
    if any(phrase in summary_lower for phrase in ("confirmed", "scheduled", "booked", "appointment")):
        return {"status": "completed", "ended_reason": ended_reason, "summary": summary}

    # Customer hung up or call ended normally
    if "customer" in reason_lower and "ended" in reason_lower:
        return {"status": "completed", "ended_reason": ended_reason, "summary": summary}

    # Default: mark as completed (call happened, outcome unclear)
    return {"status": "completed", "ended_reason": ended_reason, "summary": summary}


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
    # TTS reads "ProjectsForce" as "Project Source" — use spaced version for speech
    tts_name = name.replace("ProjectsForce", "Projects Force")
    has_transfer = bool(support_number)
    server_config: dict = {
        "url": _get_webhook_url(),
        "timeoutSeconds": 60,
    }
    if server_secret:
        server_config["secret"] = server_secret

    # Build customer/non-project handling based on whether transfer is available
    if has_transfer:
        customer_instruction = (
            "say 'I don't recognize your phone number. "
            "Let me transfer you to our team so they can help you.' "
            "Then use the transferCall tool to transfer the call."
        )
        non_project_instruction = (
            "say 'Let me connect you with someone who can help.' "
            "Then use the transferCall tool to transfer the call."
        )
    else:
        customer_instruction = (
            f"say 'I don't have your account on file right now. "
            f"Our team at {tts_name} will reach out to you shortly. "
            "Is there anything else I can help you with?' "
            "Then end the call gracefully."
        )
        non_project_instruction = (
            f"say 'Our team at {tts_name} will reach out to you shortly. "
            "Is there anything else I can help you with?' "
            "Then end the call gracefully."
        )

    system_prompt = (
        f"You are J, a friendly phone assistant for {tts_name} "
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
        "3. If RETAILER: ask for a project number or PO number. "
        "ONLY accept a project number or PO number — nothing else. "
        "Do NOT accept customer names, addresses, phone numbers, "
        "project descriptions, or any other identifier. "
        "If they give something else, say: 'I can only look up projects "
        "by project number or PO number. Do you have either of those?' "
        "Once they give a valid number, call ask_store_bot "
        "with ALL THREE fields: question (what they said), "
        "lookup_type ('project_number' or 'po_number'), and "
        "lookup_value (the number they gave). "
        "ALL THREE are REQUIRED — omitting lookup_type "
        "or lookup_value will cause authentication to FAIL.\n"
        f"4. If CUSTOMER: {customer_instruction}\n"
        "5. If their request is NOT project-related (e.g., job inquiry, "
        f"sales call, wrong number): {non_project_instruction}\n\n"
        "## HANDLING VAGUE OR CONFUSED CALLERS\n"
        "If the caller says vague things like 'I don't know', "
        "'What do you do?', 'Help me', or seems confused:\n"
        "- Do NOT immediately transfer or end the call.\n"
        "- Give a brief orientation: 'I help retailers check project status "
        "and help customers schedule their home improvement appointments. "
        "Are you a customer or calling from a retailer?'\n"
        "- If after 2 attempts they still cannot clarify, "
        f"{non_project_instruction}\n\n"
        "## AFTER RETAILER AUTHENTICATION\n"
        "Once authenticated via ask_store_bot, follow these rules:\n"
        "- The first ask_store_bot response includes project info. "
        "Present that info to the caller immediately, then ask "
        "'Is there anything else you need?'\n"
        "- Use ask_store_bot for ALL subsequent queries. "
        "The system remembers the project — just pass the caller's words "
        "in the question field.\n"
        "- Pass the user's EXACT words in the \"question\" field. "
        "Do NOT rephrase.\n"
        "- NEVER share customer names, phone numbers, "
        "email addresses, or street addresses. "
        "Only share project status, scheduled dates, and technician names.\n"
        "- NEVER offer to schedule, reschedule, or cancel appointments. "
        "Retailer callers can ONLY check project status. If they ask, "
        "say: 'Scheduling is not available for retailer calls. "
        "Please have the customer call us directly.'\n\n"
        "## CRITICAL: Never Read Numbers Aloud\n"
        "NEVER read project numbers, order numbers, IDs, or PO numbers "
        "aloud — they are long and unintelligible over the phone. "
        "Instead say 'your flooring installation' or 'your window measurement'. "
        "If the caller has one project, just say 'your project'.\n\n"
        "## CRITICAL: NEVER Fabricate Information\n"
        "ONLY share information that ask_store_bot returned in its response. "
        "If the tool returns a vague answer like 'provide a project number' or "
        "'I need more information', tell the caller exactly that — do NOT "
        "make up an answer. NEVER invent project status, scheduled dates, "
        "technician names, addresses, or any other details. "
        "If you do not have data from the tool, say "
        "'I don't have that information right now.'\n\n"
        "## GENERAL RULES\n"
        "- Keep responses concise — no bullet points, no markdown.\n"
        "- Say 'One moment.' ONLY when the caller asks a NEW question "
        "that requires a tool call. Do NOT say any filler when the caller "
        "is just replying to your question. "
        "NEVER say 'Hold on', 'Wait', 'Hang on', 'Just a sec', "
        "'Give me a moment', 'Let me check', 'Let me pull that up', "
        "'One second', or 'Let me take a look'. "
        "The ONLY allowed filler is 'One moment.' — nothing else.\n"
        "- NEVER say 'I'm transferring you now' unless you are actually "
        "invoking the transferCall tool in the same turn. If you cannot "
        "transfer, do NOT mention transferring at all.\n"
        + (
            ""
            if has_transfer
            else (
                "## CRITICAL: No Transfer Capability\n"
                "You do NOT have a transferCall tool. You CANNOT transfer calls. "
                "NEVER say 'I'm transferring you', 'Let me transfer you', "
                "'Let me connect you', or anything about transferring. "
                "You will ONLY hang up on the caller if you say these words. "
                "Instead, when you cannot help, say: "
                f"'Our team at {tts_name} will reach out to you shortly. "
                "Is there anything else I can help you with?' "
                "If they say no, end the call politely.\n"
            )
        )
        + "- If the caller says 'just a second', 'hold on', 'let me check', "
        "or similar — say 'Take your time, I'll be right here' and wait "
        "patiently. Do NOT end the call or rush them.\n"
        "- NEVER ask clarifying questions before calling the tool. "
        "Let the scheduling bot handle clarification."
        + (
            f"\n\nOFFICE HOURS: {hours_context['prompt_snippet']}"
            if hours_context and hours_context.get("prompt_snippet")
            else ""
        )
    )

    return {
        "name": f"{name} Assistant",
        "voice": _VOICE_CONFIG,
        "model": {
            "model": "gpt-4o",
            "provider": "openai",
            "temperature": 0.3,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt,
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "ask_store_bot",
                        "description": (
                            "Use for retailer callers ONLY. You MUST always include "
                            "all three parameters. Auth FAILS without lookup_type "
                            "and lookup_value."
                        ),
                        "parameters": {
                            "type": "object",
                            "required": ["question", "lookup_type", "lookup_value"],
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
                                        "Type of identifier. Use 'project_number' or 'po_number'."
                                    ),
                                },
                                "lookup_value": {
                                    "type": "string",
                                    "description": (
                                        "The project number or PO number the caller provided."
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
            f"Thank you for calling {tts_name}. Have a great day!"
        ),
        "endCallPhrases": [
            "goodbye", "bye bye", "bye now",
            "talk to you later", "have a great day",
            "have a good day",
        ],
        "endCallFunctionEnabled": True,
        "silenceTimeoutSeconds": 60,
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


async def _handle_server_event(body: dict) -> dict:
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

        # ── Outbound call end-of-call handling ──────────────────────
        outbound = get_active_call(call_id)
        # DynamoDB fallback for multi-instance
        if not outbound:
            our_call_id = (call_data.get("metadata") or {}).get("call_id", "")
            if our_call_id:
                outbound = await get_outbound_call(our_call_id)
                if outbound:
                    logger.info(
                        "End-of-call cache miss for vapi_call_id=%s — loaded from DDB call_id=%s",
                        call_id, our_call_id,
                    )
        if outbound:
            our_call_id = outbound.get("call_id", "")
            outcome = _classify_outbound_outcome(reason, summary)
            task = asyncio.create_task(
                update_outbound_call(our_call_id, {
                    "status": outcome["status"],
                    "call_result": outcome,
                })
            )
            _background_tasks.add(task)
            task.add_done_callback(_background_tasks.discard)
            remove_active_call(call_id)

            # Post call notes using cached outbound creds
            creds = outbound.get("auth_creds", {})
            if creds.get("bearer_token") and summary:
                session_id = f"vapi-{call_id}"
                task2 = asyncio.create_task(
                    post_call_summary_notes(
                        session_id=session_id,
                        bearer_token=creds["bearer_token"],
                        client_id=creds.get("client_id", ""),
                        customer_id=creds.get("customer_id", ""),
                        summary=summary,
                        duration_seconds=duration,
                    )
                )
                _background_tasks.add(task2)
                task2.add_done_callback(_background_tasks.discard)

            # Retry on alternate number if no_answer/voicemail
            if outcome["status"] in ("no_answer", "voicemail"):
                retry_task = asyncio.create_task(retry_outbound_call(outbound))
                _background_tasks.add(retry_task)
                retry_task.add_done_callback(_background_tasks.discard)

            _call_project_pin.pop(call_id, None)
            logger.info(
                "Outbound call ended: our_call_id=%s status=%s reason=%s",
                our_call_id, outcome["status"], reason,
            )
            return {"status": "ok"}

        # ── Incomplete reschedule detection + auto-recovery ─────────
        session_id_check = f"vapi-{call_id}"
        session_projects = get_session_projects(session_id_check)
        phone_number = _extract_phone_number(call_data)
        for pid in list(session_projects.keys()):
            if pid in _reschedule_pending:
                old_appt = get_reschedule_old_appointment(pid)
                if old_appt and old_appt.get("date"):
                    # Try to get creds for auto-recovery
                    recovery_creds = None
                    if phone_number:
                        recovery_creds = get_cached_auth(normalize_phone(phone_number))
                    if not recovery_creds:
                        recovery_creds = _call_auth_cache.get(call_id)

                    if recovery_creds and recovery_creds.get("bearer_token"):
                        logger.warning(
                            "INCOMPLETE RESCHEDULE — attempting auto-recovery: "
                            "project=%s old=%s call_id=%s",
                            pid, old_appt, call_id,
                        )
                        task = asyncio.create_task(
                            _attempt_reschedule_recovery(pid, old_appt, recovery_creds, call_id)
                        )
                        _background_tasks.add(task)
                        task.add_done_callback(_background_tasks.discard)
                    else:
                        logger.error(
                            "INCOMPLETE RESCHEDULE — no creds for recovery: "
                            "project=%s old_appointment=%s call_id=%s "
                            "— manual recovery required.",
                            pid, old_appt, call_id,
                        )
                else:
                    logger.error(
                        "INCOMPLETE RESCHEDULE — no old appointment cached: "
                        "project=%s call_id=%s — manual recovery required.",
                        pid, call_id,
                    )
                _reschedule_pending.discard(pid)
                clear_reschedule_old_appointment(pid)

        # ── Inbound call end-of-call handling (existing) ────────────
        # Post call summary notes to discussed projects (fire-and-forget).
        # IMPORTANT: extract project/notes data BEFORE cleanup_call_caches()
        # clears it — the async tasks run later when the data would be gone.
        session_id = f"vapi-{call_id}"
        session_key = f"vapi-{call_id}"
        store_session = _store_sessions.get(session_key)

        # Snapshot session data before cleanup
        session_projects = get_session_projects(session_id)
        session_notes = get_session_notes(session_id)

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
                    projects_discussed=session_projects,
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
                            projects_discussed=session_projects,
                            cached_notes=session_notes,
                        )
                    )
                    _background_tasks.add(task)
                    task.add_done_callback(_background_tasks.discard)
                else:
                    logger.warning(
                        "No cached creds for call notes (call_id=%s phone=***%s)",
                        call_id, phone_number[-4:] if phone_number else "none",
                    )

        # Clean up all caches for this call (safe — data already snapshotted above)
        cleanup_call_caches(session_id)
        _store_sessions.pop(session_key, None)
        _call_auth_cache.pop(call_id, None)
        _call_project_pin.pop(call_id, None)
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

        # Inject pinned project context so our Claude agent stays on track.
        # Once a project is identified in a call, every subsequent question
        # includes it — preventing GPT/Claude from drifting to another project.
        pinned = _call_project_pin.get(call_id)
        if pinned:
            # Outbound calls are always about ONE project (from SQS).
            # Inbound calls may involve multiple projects — guide but don't lock.
            outbound = get_active_call(call_id)
            if outbound:
                question = (
                    f"[CONTEXT: This call is about project_id={pinned}. "
                    f"Use ONLY this project for all tool calls.]\n{question}"
                )
            else:
                question = (
                    f"[CONTEXT: The customer was last discussing project_id={pinned}. "
                    f"When they say 'this project', 'same project', 'cancel this', "
                    f"'schedule this', etc. — use project_id={pinned}. "
                    f"Only switch if they explicitly mention a different project "
                    f"by name or number.]\n{question}"
                )

        logger.info(
            "Vapi ask_scheduling_bot question: call_id=%s q=%s",
            call_id, question[:500],
        )

        agent_name = ""
        start_time = time.monotonic()
        try:
            reset_action_flags()
            reset_request_caches()
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

            # Pin/update project for the call when any tool uses a project_id.
            # - Outbound calls: pinned at assistant-request, never overwritten here.
            # - Inbound calls: tracks the active project. Updates if the customer
            #   explicitly switches to a different project (e.g., "now schedule
            #   my other project"). Context injection guides vague references
            #   ("this project", "same project") to the pinned project.
            used_project = get_last_project_id()
            if used_project:
                is_outbound = bool(get_active_call(call_id))
                prev_pin = _call_project_pin.get(call_id)
                if not prev_pin:
                    _call_project_pin[call_id] = used_project
                    logger.info("Pinned project_id=%s for call_id=%s", used_project, call_id)
                elif not is_outbound and prev_pin != used_project:
                    # Inbound: customer switched projects explicitly
                    logger.info(
                        "Project switch %s → %s for call_id=%s",
                        prev_pin, used_project, call_id,
                    )
                    _call_project_pin[call_id] = used_project

            # Guardrail: detect hallucinated write actions via intent classifier.
            #
            # Instead of brittle pattern lists, we ask the LLM to classify
            # whether the response claims a write action was completed.
            # Only invoked when at least one write-action flag is False.
            #
            # IMPORTANT: Skip the guardrail if the action already succeeded
            # for this project earlier in the same call session.  The per-
            # request ContextVar flags reset every turn, so on the NEXT turn
            # Claude correctly saying "already confirmed" would trigger a
            # false-positive retry that creates duplicate bookings.
            retry_prompt = ""
            pinned = get_last_project_id() or _call_project_pin.get(call_id, "")

            # Quick-check: if all write-action flags are True, skip classifier
            uncalled_actions = set()
            if not was_confirm_called():
                uncalled_actions.add("confirm")
            if not was_cancel_called():
                uncalled_actions.add("cancel")
            if not was_note_added():
                uncalled_actions.add("note")
            if not was_address_updated():
                uncalled_actions.add("address")

            # Also check fabricated time slots (pattern-based, not intent)
            if not was_time_slots_called() and _looks_like_time_slot_list(voice_text):
                logger.warning(
                    "Fabricated time slots in Vapi call (call_id=%s) — retrying",
                    call_id,
                )
                if _looks_like_date_selection(question):
                    retry_prompt = (
                        f"You fabricated time slots. The customer selected '{question}'. "
                        "You MUST call get_time_slots NOW for that date to get the REAL "
                        "available time slots. Do NOT re-list dates — the customer already "
                        "chose a date. Call get_time_slots and present ONLY the slots it returns."
                    )
                else:
                    retry_prompt = (
                        "You listed time slots but you did NOT call the get_time_slots tool. "
                        "Those time slots are FABRICATED and WRONG. Remove ALL time slot "
                        "mentions from your response. Present ONLY the available dates. "
                        "Once the customer picks a date, THEN call get_time_slots to get "
                        "real time slots."
                    )

            # Intent classifier: only run if there are uncalled actions AND
            # no time-slot retry already triggered
            if uncalled_actions and not retry_prompt:
                claimed = await _classify_claimed_actions_async(voice_text)
                # Intersect: which actions were claimed but NOT actually called?
                hallucinated = claimed & uncalled_actions
                # Filter out actions that already succeeded in this session
                if "confirm" in hallucinated and (
                    session_action_completed(session_id, "confirm", pinned)
                    or session_has_any_completed(session_id, "confirm")
                ):
                    hallucinated.discard("confirm")
                if "cancel" in hallucinated and (
                    session_action_completed(session_id, "cancel", pinned)
                    or session_has_any_completed(session_id, "cancel")
                ):
                    hallucinated.discard("cancel")
                if "address" in hallucinated and (
                    session_action_completed(session_id, "address_update", pinned)
                    or session_has_any_completed(session_id, "address_update")
                ):
                    hallucinated.discard("address")

                if hallucinated:
                    logger.warning(
                        "Guardrail classifier detected hallucinated actions %s "
                        "in Vapi call (call_id=%s) — retrying",
                        hallucinated, call_id,
                    )
                    # Build retry prompt based on what was hallucinated
                    if "confirm" in hallucinated:
                        retry_prompt = (
                            "The customer confirmed. You MUST call the confirm_appointment "
                            "tool NOW to actually book the appointment. Do NOT respond "
                            "without calling the tool."
                        )
                    elif "cancel" in hallucinated:
                        retry_prompt = (
                            "You said the appointment was cancelled but you did NOT call "
                            "cancel_appointment. The appointment is NOT actually cancelled. "
                            "You MUST call cancel_appointment(project_id, reason) NOW. "
                            "Use the reason the customer already provided."
                        )
                    elif "address" in hallucinated:
                        retry_prompt = (
                            "You told the customer the address was saved/noted but you did NOT call "
                            "update_installation_address. The address is NOT saved. "
                            "You MUST call update_installation_address NOW with the address details "
                            "the customer provided. After that, call add_note starting with "
                            "'CUSTOMER REQUESTED INSTALLATION ADDRESS UPDATE'. "
                            "Do NOT tell the customer the address is saved until BOTH tools succeed."
                        )
                    elif "note" in hallucinated:
                        retry_prompt = (
                            "You told the customer the note was added/saved but you did NOT call "
                            "add_note. The note is NOT saved. "
                            "You MUST call add_note NOW with the note text "
                            "the customer provided. Do NOT tell the customer the note is saved "
                            "until add_note succeeds."
                        )

            if retry_prompt:
                # Pin the project so GPT cannot drift to a different project
                # during the retry (fixes project-switching bug).
                pinned_project = get_last_project_id()
                if pinned_project:
                    retry_prompt = (
                        f"IMPORTANT: You are working on project_id={pinned_project}. "
                        f"Do NOT switch to any other project. Use ONLY project_id="
                        f"{pinned_project} for all tool calls. " + retry_prompt
                    )
                # Suppress LLM self-correction text
                retry_prompt += (
                    " Respond ONLY with the correct answer. "
                    "Do NOT apologize or explain what went wrong."
                )
                reset_action_flags()
                response = await orchestrator.route_request(
                    user_input=retry_prompt,
                    user_id=user_id,
                    session_id=session_id,
                    additional_params={"channel": "vapi"},
                )
                retry_text = extract_response_text(response.output)
                if was_confirm_called() or was_cancel_called() or was_time_slots_called() or was_address_updated() or was_note_added():
                    voice_text = format_for_voice(retry_text)
                    logger.info("Vapi retry succeeded — required tool was called")
                elif not _looks_like_time_slot_list(retry_text):
                    # Retry didn't call the tool but also didn't fabricate — accept it
                    voice_text = format_for_voice(retry_text)
                    logger.info("Vapi retry succeeded — no fabricated slots in response")
                else:
                    # Retry still has fabricated slots — strip them as last resort
                    logger.warning("Vapi retry also fabricated slots — stripping them")
                    voice_text = _strip_time_slots(voice_text)
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

    # Direct outbound tools — bypass orchestrator, call scheduling functions directly
    if tool_name in ("get_time_slots", "confirm_appointment", "add_note", "get_available_dates"):
        return await _handle_outbound_direct_tool(
            tool_name, tool_params, call_id, tool_call_id, session_id, user_id,
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
        store_session["just_authenticated"] = True
        store_session["lookup_type"] = lookup_type
        store_session["lookup_value"] = lookup_value
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
    # On first call after auth, override question to list projects —
    # the caller's original question was "PO is 74356" which the scheduling
    # agent can't act on.  Show all projects + status immediately.
    just_authed = store_session.pop("just_authenticated", False)
    if just_authed:
        # Include lookup info so the agent can find the specific project
        lookup_hint = ""
        auth_lookup_type = store_session.get("lookup_type", "")
        auth_lookup_value = store_session.get("lookup_value", "")
        if auth_lookup_type and auth_lookup_value:
            lookup_hint = (
                f" The caller authenticated with {auth_lookup_type.replace('_', ' ')} "
                f"{auth_lookup_value} — find the matching project and show its details "
                f"(status, scheduled date, technician). "
                f"If multiple projects exist, focus on the one matching this identifier."
            )
        store_question = (
            "[STORE CALLER — status and technician names only, no scheduling, no customer PII] "
            "List this customer's projects and show their current status." + lookup_hint
        )
    else:
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


async def _handle_outbound_direct_tool(
    tool_name: str,
    tool_params: dict,
    call_id: str,
    tool_call_id: str,
    session_id: str,
    user_id: str,
) -> dict:
    """Handle direct tool calls for outbound calls — bypass orchestrator.

    Calls scheduling functions directly with the project_id from the
    active outbound call cache.  No classifier, no scheduling agent —
    just a direct function call.
    """
    outbound = get_active_call(call_id)
    if not outbound:
        logger.warning("Direct tool %s but no active outbound call (call_id=%s)", tool_name, call_id)
        return _build_tool_result(
            "I'm having trouble with that. Let me try again.", tool_call_id,
        )

    project_id = outbound.get("project_id", "")
    if not project_id:
        logger.error("No project_id in outbound call data (call_id=%s)", call_id)
        return _build_tool_result(
            "I'm having trouble with that. Let me try again.", tool_call_id,
        )

    start_time = time.monotonic()
    try:
        if tool_name == "get_time_slots":
            date = tool_params.get("date", "")
            result = await sched_get_time_slots(project_id, date)
        elif tool_name == "confirm_appointment":
            date = tool_params.get("date", "")
            time_slot = tool_params.get("time", "")
            result = await sched_confirm_appointment(project_id, date, time_slot)
        elif tool_name == "add_note":
            note_text = tool_params.get("note_text", "")
            result = await sched_add_note(project_id, note_text)
        elif tool_name == "get_available_dates":
            result = await sched_get_available_dates(project_id)
        else:
            result = f"Unknown tool: {tool_name}"
    except Exception:
        logger.exception("Direct tool %s failed (call_id=%s)", tool_name, call_id)
        result = "I'm having trouble with that right now. Let me try again."

    elapsed_ms = int((time.monotonic() - start_time) * 1000)
    voice_text = format_for_voice(result)

    logger.info(
        "Outbound direct tool: call_id=%s tool=%s elapsed_ms=%d chars=%d",
        call_id, tool_name, elapsed_ms, len(voice_text),
    )

    # Log conversation for tracking
    task = asyncio.create_task(
        log_conversation(
            session_id=session_id,
            user_id=user_id,
            user_message=f"[{tool_name}] {json.dumps(tool_params)}",
            bot_response=voice_text,
            agent_name="direct_tool",
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

    # If this is an active outbound call, use pre-cached auth creds
    outbound = get_active_call(call_id)

    # DynamoDB fallback: if cache miss (e.g. multi-instance ALB routing),
    # use metadata.call_id to look up the outbound record from DynamoDB
    if not outbound:
        our_call_id = (call_data.get("metadata") or {}).get("call_id", "")
        if our_call_id:
            outbound = await get_outbound_call(our_call_id)
            if outbound:
                logger.info(
                    "Outbound cache miss for vapi_call_id=%s — loaded from DDB call_id=%s",
                    call_id, our_call_id,
                )
                cache_active_call(call_id, outbound)

    if outbound and outbound.get("auth_creds"):
        creds = outbound["auth_creds"]
        AuthContext.set(
            auth_token=creds.get("bearer_token", ""),
            client_id=creds.get("client_id", ""),
            customer_id=creds.get("customer_id", creds.get("user_id", "")),
            user_id=creds.get("user_id", ""),
            user_name=creds.get("user_name", ""),
            timezone=creds.get("timezone", "US/Eastern"),
            support_number=creds.get("support_number", ""),
        )
        return

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


async def _attempt_reschedule_recovery(
    project_id: str,
    old_appt: dict,
    creds: dict,
    call_id: str,
) -> None:
    """Attempt to rebook the original appointment after an incomplete reschedule.

    When a reschedule flow is interrupted (caller hangs up, call drops),
    the old appointment has already been cancelled.  This function tries to
    rebook the original date/time so the customer doesn't lose their slot.
    """
    AuthContext.set(
        auth_token=creds.get("bearer_token", ""),
        client_id=creds.get("client_id", ""),
        customer_id=str(creds.get("customer_id", "")),
    )
    raw_date = old_appt.get("date", "")
    old_time = old_appt.get("time", "")

    if not raw_date:
        logger.error(
            "RESCHEDULE RECOVERY SKIP: project=%s — no date in old appointment",
            project_id,
        )
        return

    # Parse the cached date — may be "04-24-2026 08:00 AM" or "2026-04-24"
    old_date = raw_date
    for fmt in ("%m-%d-%Y %I:%M %p", "%m-%d-%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw_date.strip(), fmt)
            old_date = parsed.strftime("%Y-%m-%d")
            if not old_time and fmt == "%m-%d-%Y %I:%M %p":
                old_time = parsed.strftime("%I:%M %p").lstrip("0")
            break
        except ValueError:
            continue

    logger.info(
        "RESCHEDULE RECOVERY: parsed date=%s time=%s from raw=%s",
        old_date, old_time, raw_date,
    )

    try:
        # Ensure we have available dates cached (primes _request_id_by_project)
        await sched_get_available_dates(project_id)

        result = await sched_confirm_appointment(project_id, old_date, old_time)
        result_lower = result.lower() if isinstance(result, str) else ""

        if "submit" in result_lower or "schedul" in result_lower or "confirm" in result_lower:
            logger.info(
                "RESCHEDULE RECOVERY SUCCESS: project=%s rebooked at %s %s call_id=%s",
                project_id, old_date, old_time, call_id,
            )
            await sched_add_note(
                project_id,
                f"AUTO-RECOVERY: Original appointment ({old_date} {old_time}) "
                "was restored after incomplete reschedule during phone call.",
            )
        else:
            logger.error(
                "RESCHEDULE RECOVERY FAILED: project=%s result=%s call_id=%s",
                project_id, result, call_id,
            )
            await sched_add_note(
                project_id,
                f"INCOMPLETE RESCHEDULE: Original appointment ({old_date} {old_time}) "
                f"was cancelled but could not be restored. "
                f"Recovery result: {result[:200]}. Manual recovery required.",
            )
    except Exception:
        logger.exception(
            "RESCHEDULE RECOVERY ERROR: project=%s call_id=%s", project_id, call_id,
        )
        try:
            await sched_add_note(
                project_id,
                f"INCOMPLETE RESCHEDULE: Original appointment ({old_date} {old_time}) "
                "was cancelled but auto-recovery failed with an error. "
                "Manual recovery required.",
            )
        except Exception:
            logger.exception("Failed to add recovery-failure note for project=%s", project_id)


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
