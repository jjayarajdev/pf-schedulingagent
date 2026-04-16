"""Vapi Custom LLM endpoint — POST /vapi/chat/completions.

Replaces GPT-4o-mini as the reasoning LLM for authenticated customer calls.
Vapi handles telephony + STT + TTS; our Bedrock Claude handles ALL reasoning
via the AgentSquad orchestrator — identical to web chat.

BEFORE: User → Vapi STT → GPT-4o-mini → ask_scheduling_bot → Our Claude → response → GPT speaks
AFTER:  User → Vapi STT → POST /vapi/chat/completions → Our Claude → SSE stream → Vapi TTS
"""

import asyncio
import json
import logging
import random
import re
import time
import uuid
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from auth.context import AuthContext
from channels.conversation_log import log_conversation
from channels.formatters import format_for_voice
from channels.vapi import _normalize_e164, get_call_auth
from config import get_secrets
from observability.logging import RequestContext
from orchestrator import get_orchestrator
from orchestrator.response_utils import extract_response_text
from tools.scheduling import (
    reset_action_flags,
    reset_request_caches,
    was_cancel_called,
    was_confirm_called,
    was_time_slots_called,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/vapi", tags=["vapi"])

# Background tasks need a strong reference to avoid GC before completion
_background_tasks: set[asyncio.Task] = set()

_FALLBACK_MESSAGE = (
    "I'm having trouble looking that up right now. "
    "Let me connect you with our support team."
)

# ── Filler phrases ────────────────────────────────────────────────────────

_FILLERS = [
    "One moment, let me check that.",
    "Let me look that up for you.",
    "Give me just a second.",
    "Let me pull that up.",
    "One moment please.",
]

_filler_index = 0


def _rotating_filler() -> str:
    """Return a varied filler phrase — rotates sequentially to avoid repeats."""
    global _filler_index
    filler = _FILLERS[_filler_index % len(_FILLERS)]
    _filler_index += 1
    return filler


# ── Transfer detection ────────────────────────────────────────────────────

_TRANSFER_PATTERNS = [
    "transfer you",
    "transferring you",
    "connect you to",
    "connecting you to",
    "connect you with",
    "connecting you with",
    "let me connect you",
    "i'll connect you",
]


def _wants_transfer(text: str) -> bool:
    """Detect if the response indicates the caller should be transferred."""
    lower = text.lower()
    return any(p in lower for p in _TRANSFER_PATTERNS)


# ── Guardrail patterns (same as vapi.py) ──────────────────────────────────

_BOOKING_CONFIRMATION_PATTERNS = [
    "appointment confirmed",
    "appointment is now confirmed",
    "is now scheduled",
    "has been scheduled",
    "has been successfully scheduled",
    "successfully scheduled",
    "appointment has been booked",
    "you're all set",
    "all set",
    "your appointment is confirmed",
    "booking confirmed",
    "is booked",
    "you're scheduled",
    "have been booked",
]

_CANCEL_CONFIRMATION_PATTERNS = [
    "has been cancelled",
    "has been canceled",
    "appointment cancelled",
    "appointment canceled",
    "successfully cancelled",
    "successfully canceled",
    "i've cancelled",
    "i've canceled",
    "cancellation is complete",
    "appointment has been removed",
]

_TIME_SLOT_PATTERN = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)\b")


def _looks_like_booking_confirmation(text: str) -> bool:
    lower = text.lower()
    return any(p in lower for p in _BOOKING_CONFIRMATION_PATTERNS)


def _looks_like_cancel_confirmation(text: str) -> bool:
    lower = text.lower()
    return any(p in lower for p in _CANCEL_CONFIRMATION_PATTERNS)


def _looks_like_time_slot_list(text: str) -> bool:
    """Detect if the response contains a list of time slots (3+ AM/PM times)."""
    matches = _TIME_SLOT_PATTERN.findall(text)
    return len(matches) >= 3


# ── OpenAI SSE chunk builders ─────────────────────────────────────────────


def _openai_chunk(chunk_id: str, content: str, role: str | None = None) -> str:
    """Format one SSE data line in OpenAI chat.completion.chunk format."""
    delta: dict = {}
    if role:
        delta["role"] = role
    if content:
        delta["content"] = content

    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _openai_done_chunk(chunk_id: str, finish_reason: str = "stop") -> str:
    """Format the final SSE chunk with finish_reason."""
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n"


def _openai_transfer_chunk(chunk_id: str, destination: str) -> str:
    """Format a transferCall tool_call SSE chunk for Vapi to execute."""
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"call_{uuid.uuid4().hex[:24]}",
                            "type": "function",
                            "function": {
                                "name": "transferCall",
                                "arguments": json.dumps({"destination": destination}),
                            },
                        }
                    ],
                },
                "finish_reason": None,
            }
        ],
    }
    return f"data: {json.dumps(payload)}\n\n"


# ── TTS helpers ───────────────────────────────────────────────────────────


def _split_for_tts(text: str) -> list[str]:
    """Split text on sentence boundaries for natural TTS pacing."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    return [s for s in sentences if s.strip()]


def _extract_last_user_message(messages: list[dict]) -> str:
    """Extract the last user message content from an OpenAI-format messages array."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                    elif isinstance(part, str):
                        text_parts.append(part)
                return " ".join(text_parts)
            return str(content)
    return ""


# ── Endpoint ──────────────────────────────────────────────────────────────


@router.post(
    "/chat/completions",
    summary="Vapi Custom LLM endpoint",
)
async def vapi_chat_completions(request: Request, secret: str = Query("")):
    """Vapi Custom LLM — streams OpenAI-compatible SSE responses.

    Replaces GPT-4o-mini for authenticated customer calls.  Our Bedrock Claude
    handles all reasoning via the AgentSquad orchestrator, producing the same
    quality as web chat.
    """
    # Validate secret query parameter (Vapi doesn't send x-vapi-secret to Custom LLM URLs)
    # Vapi URL-encodes the query parameter value, so decode it before comparing
    expected = get_secrets().vapi_api_key
    decoded_secret = unquote(secret) if secret else ""
    logger.info(
        "Custom LLM auth: raw_len=%d decoded_len=%d expected_len=%d match=%s",
        len(secret),
        len(decoded_secret),
        len(expected),
        decoded_secret == expected,
    )
    if not expected or not decoded_secret or decoded_secret != expected:
        logger.warning(
            "Custom LLM auth FAILED: decoded_preview=%s expected_preview=%s",
            repr(decoded_secret[:8]) if decoded_secret else "(empty)",
            repr(expected[:8]) if expected else "(empty)",
        )
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()

    # Extract call metadata (Vapi sends this with metadataSendMode: "variable")
    call_data = body.get("call", {})
    call_id = call_data.get("id", "")
    messages = body.get("messages", [])

    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    session_id = f"vapi-{call_id}" if call_id else f"vapi-{uuid.uuid4().hex[:8]}"

    # Look up cached auth from assistant-request
    creds = get_call_auth(call_id) if call_id else None
    if creds:
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
    else:
        logger.warning("No cached auth for call_id=%s", call_id)

    phone_number = call_data.get("customer", {}).get("number", "")
    user_id = phone_number or (creds.get("user_id", "vapi-anonymous") if creds else "vapi-anonymous")

    RequestContext.set(session_id=session_id, user_id=user_id, channel="vapi")

    user_message = _extract_last_user_message(messages)
    if not user_message:
        logger.warning("No user message in chat/completions (call_id=%s)", call_id)

        async def _empty():
            yield _openai_chunk(chunk_id, "I didn't catch that. Could you repeat?", role="assistant")
            yield _openai_done_chunk(chunk_id)
            yield "data: [DONE]\n\n"

        return StreamingResponse(_empty(), media_type="text/event-stream")

    logger.info(
        "Vapi Custom LLM: call_id=%s user_message=%s",
        call_id,
        user_message[:200],
    )

    support_number = creds.get("support_number", "") if creds else ""

    async def _stream():
        # Emit filler first — Vapi speaks this immediately while we process
        filler = _rotating_filler()
        yield _openai_chunk(chunk_id, f"{filler}<flush />", role="assistant")

        agent_name = ""
        voice_text = _FALLBACK_MESSAGE
        start_time = time.monotonic()
        try:
            reset_action_flags()
            reset_request_caches()
            orchestrator = get_orchestrator()
            response = await orchestrator.route_request(
                user_input=user_message,
                user_id=user_id,
                session_id=session_id,
                additional_params={"channel": "vapi"},
            )
            response_text = extract_response_text(response.output)
            voice_text = format_for_voice(response_text)
            agent_name = response.metadata.agent_name if response.metadata else ""

            # ── Guardrails: detect hallucinated actions ─────────
            retry_prompt = ""
            if not was_confirm_called() and _looks_like_booking_confirmation(voice_text):
                logger.warning(
                    "Hallucinated booking (Custom LLM, call_id=%s) — retrying",
                    call_id,
                )
                retry_prompt = (
                    "The customer confirmed. You MUST call the confirm_appointment "
                    "tool NOW to actually book the appointment. Do NOT respond "
                    "without calling the tool."
                )
            elif not was_cancel_called() and _looks_like_cancel_confirmation(voice_text):
                logger.warning(
                    "Hallucinated cancellation (Custom LLM, call_id=%s) — retrying",
                    call_id,
                )
                retry_prompt = (
                    "You said the appointment was cancelled but you did NOT call "
                    "cancel_appointment. The appointment is NOT actually cancelled. "
                    "You MUST call cancel_appointment(project_id, reason) NOW."
                )
            elif not was_time_slots_called() and _looks_like_time_slot_list(voice_text):
                logger.warning(
                    "Fabricated time slots (Custom LLM, call_id=%s) — retrying",
                    call_id,
                )
                retry_prompt = (
                    "You listed time slots but you did NOT call the get_time_slots tool. "
                    "Those time slots are FABRICATED. You MUST call "
                    "get_time_slots(project_id, date) NOW to get real time slots."
                )

            if retry_prompt:
                reset_action_flags()
                response = await orchestrator.route_request(
                    user_input=retry_prompt,
                    user_id=user_id,
                    session_id=session_id,
                    additional_params={"channel": "vapi"},
                )
                retry_text = extract_response_text(response.output)
                if was_confirm_called() or was_cancel_called() or was_time_slots_called():
                    voice_text = format_for_voice(retry_text)
                    logger.info("Custom LLM retry succeeded — required tool was called")
                else:
                    logger.warning("Custom LLM retry also failed to call the required tool")

        except Exception:
            logger.exception("Orchestrator error in Custom LLM (call_id=%s)", call_id)
            voice_text = _FALLBACK_MESSAGE

        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        logger.info(
            "Custom LLM result: call_id=%s chars=%d elapsed_ms=%d agent=%s",
            call_id,
            len(voice_text),
            elapsed_ms,
            agent_name,
        )

        # Check for transfer intent
        if _wants_transfer(voice_text) and support_number:
            e164 = _normalize_e164(support_number)
            yield _openai_chunk(
                chunk_id,
                "I'm transferring you now. You'll hear ringing while I connect you.",
            )
            yield _openai_transfer_chunk(chunk_id, e164)
            yield _openai_done_chunk(chunk_id, "tool_calls")
            yield "data: [DONE]\n\n"
        else:
            # Stream response in sentence chunks for natural TTS pacing
            sentences = _split_for_tts(voice_text)
            for sentence in sentences:
                yield _openai_chunk(chunk_id, sentence + " ")

            yield _openai_done_chunk(chunk_id)
            yield "data: [DONE]\n\n"

        # Fire-and-forget: log conversation
        task = asyncio.create_task(
            log_conversation(
                session_id=session_id,
                user_id=user_id,
                user_message=user_message,
                bot_response=voice_text,
                agent_name=agent_name,
                channel="vapi",
                response_time_ms=elapsed_ms,
            )
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    return StreamingResponse(_stream(), media_type="text/event-stream")
