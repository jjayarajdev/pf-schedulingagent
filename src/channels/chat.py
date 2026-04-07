"""Chat channel — POST /chat and POST /chat/stream (SSE)."""

import asyncio
import json
import logging
import re
import time
import uuid

from agent_squad.agents import AgentStreamResponse
from agent_squad.types import ConversationMessage, ParticipantRole
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from auth.context import AuthContext
from channels.conversation_log import log_conversation
from channels.schemas import ChatRequest, ChatResponse
from config import get_settings
from observability.logging import RequestContext
from orchestrator import get_orchestrator
from orchestrator.response_utils import extract_response_text
from orchestrator.welcome import handle_welcome
from tools.scheduling import reset_confirm_flag, was_confirm_called

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["chat"])

# Background tasks need a strong reference to avoid GC before completion
_background_tasks: set[asyncio.Task] = set()

_WELCOME_TRIGGER = "__WELCOME__"

# Maps AgentSquad agent names → v1.2.9 intent values
_AGENT_TO_INTENT = {
    "Scheduling Agent": "scheduling",
    "Chitchat Agent": "chitchat",
    "Weather Agent": "information",
    "Welcome": "welcome",
}


def _setup_request_context(
    request: ChatRequest, raw_request: Request,
) -> tuple[str, str]:
    """Set AuthContext + RequestContext from the incoming request.

    Returns ``(session_id, user_id)``.
    """
    session_id = request.session_id or str(uuid.uuid4())
    user_id = request.user_id or request.pf_user_id or "anonymous"

    # Accept both canonical and pf_-prefixed field names from the PF web app
    auth_token = request.auth_token or request.pf_token
    if not auth_token:
        auth_header = raw_request.headers.get("authorization", "")
        auth_token = auth_header.removeprefix("Bearer ").strip() if auth_header else ""

    AuthContext.set(
        auth_token=auth_token,
        client_id=request.client_id or request.pf_client_id or "",
        customer_id=request.customer_id or "",
        user_id=user_id,
        user_name=request.user_name or request.pf_user_name or "",
    )
    RequestContext.set(session_id=session_id, user_id=user_id)
    return session_id, user_id


def _repair_json_blocks(text: str) -> str:
    """Ensure all ```json blocks in the response are properly closed.

    The LLM sometimes truncates output before emitting the closing ```,
    leaving the frontend unable to parse the JSON block.  This repairs
    truncated JSON by closing open structures and appending ```.
    """
    # Fast path: no JSON blocks at all
    if "```json" not in text:
        return text

    # Check if all opened blocks are closed
    opens = [m.start() for m in re.finditer(r"```json", text)]
    closes = [m.start() for m in re.finditer(r"```(?!json)", text)]
    # Remove closes that appear before any open
    if opens and closes:
        closes = [c for c in closes if c > opens[0]]

    if len(opens) <= len(closes):
        return text  # All blocks properly closed

    # Find the unclosed block (last open without a matching close)
    last_open = opens[-1]
    json_start = text.index("\n", last_open) + 1 if "\n" in text[last_open:] else last_open + 7
    json_body = text[json_start:]

    # Try to repair the truncated JSON
    repaired = _close_truncated_json(json_body)
    if repaired:
        return text[:json_start] + repaired + "\n```"

    # Fallback: just close the block as-is
    return text + "\n```"


def _close_truncated_json(json_str: str) -> str | None:
    """Attempt to close truncated JSON by balancing braces/brackets."""
    # Count open/close structures
    braces = 0
    brackets = 0
    in_string = False
    escape = False

    for c in json_str:
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            braces += 1
        elif c == "}":
            braces -= 1
        elif c == "[":
            brackets += 1
        elif c == "]":
            brackets -= 1

    if braces == 0 and brackets == 0 and not in_string:
        return json_str  # Already valid

    result = json_str

    # Close open string
    if in_string:
        result += '"'

    # Remove trailing incomplete tokens
    result = re.sub(r',\s*$', '', result)
    result = re.sub(r':\s*$', ': null', result)
    result = re.sub(r',\s*"[^"]*"\s*$', '', result)
    result = re.sub(r',\s*"[^"]*"\s*:\s*$', '', result)

    # Close brackets and braces
    # Re-count after repairs
    braces = 0
    brackets = 0
    in_string = False
    escape = False
    for c in result:
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            braces += 1
        elif c == "}":
            braces -= 1
        elif c == "[":
            brackets += 1
        elif c == "]":
            brackets -= 1

    while brackets > 0:
        result += "]"
        brackets -= 1
    while braces > 0:
        result += "}"
        braces -= 1

    # Verify it parses
    try:
        json.loads(result)
        return result
    except json.JSONDecodeError:
        # Try trimming to last complete object in array
        last_obj = result.rfind("},")
        if last_obj > 0:
            trimmed = result[:last_obj + 1]
            # Re-close
            b2 = sum(1 for c in trimmed if c == "{") - sum(1 for c in trimmed if c == "}")
            k2 = sum(1 for c in trimmed if c == "[") - sum(1 for c in trimmed if c == "]")
            while k2 > 0:
                trimmed += "]"
                k2 -= 1
            while b2 > 0:
                trimmed += "}"
                b2 -= 1
            try:
                json.loads(trimmed)
                return trimmed
            except json.JSONDecodeError:
                pass

    return None


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
    "address has been updated",
    "address updated successfully",
    "address has been changed",
    "installation address has been updated",
]

# Patterns that indicate the LLM fabricated a scheduling failure instead of calling the tool
_FABRICATED_FAILURE_PATTERNS = [
    "system issue",
    "scheduling conflict",
    "unable to process",
    "unable to schedule",
    "couldn't process the scheduling",
    "couldn't complete the scheduling",
    "status conflict",
    "project status",
]

# Short affirmative messages that likely mean "confirm the appointment"
_AFFIRMATIVE_PATTERNS = re.compile(
    r"^(yes|yeah|yep|yup|sure|confirm|go ahead|do it|ok|okay|absolutely|please|book it)\b",
    re.IGNORECASE,
)


def _looks_like_booking_confirmation(text: str) -> bool:
    """Check if the response text claims a booking was made."""
    lower = text.lower()
    return any(p in lower for p in _BOOKING_CONFIRMATION_PATTERNS)


def _looks_like_fabricated_failure(text: str, user_message: str) -> bool:
    """Check if the LLM fabricated a scheduling failure after a user confirmation."""
    if not _AFFIRMATIVE_PATTERNS.match(user_message.strip()):
        return False
    lower = text.lower()
    return any(p in lower for p in _FABRICATED_FAILURE_PATTERNS)


_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)


def _group_time_slots(display_slots: list[str]) -> dict:
    """Group display-format time slots into morning/afternoon/evening."""
    morning: list[str] = []
    afternoon: list[str] = []
    evening: list[str] = []

    for slot in display_slots:
        try:
            parts = slot.strip().upper()
            if "AM" in parts:
                morning.append(slot)
            elif "PM" in parts:
                # Parse hour to distinguish afternoon vs evening
                hour_str = slot.split(":")[0].strip()
                hour = int(hour_str)
                if hour == 12 or hour < 5:
                    afternoon.append(slot)
                else:
                    evening.append(slot)
            else:
                morning.append(slot)
        except (ValueError, IndexError):
            morning.append(slot)

    return {
        "morning": {"label": "Morning", "slots": morning, "count": len(morning)},
        "afternoon": {"label": "Afternoon", "slots": afternoon, "count": len(afternoon)},
        "evening": {"label": "Evening", "slots": evening, "count": len(evening)},
    }


def _enrich_json_block(response_text: str) -> str:
    """Enrich the LLM's JSON block with frontend-required fields.

    Adds ``timeSlotsGrouped`` and ``slotCount`` when time slot data is present,
    since the LLM may not preserve these from tool output.
    """
    match = _JSON_BLOCK_RE.search(response_text)
    if not match:
        return response_text

    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, AttributeError):
        return response_text

    modified = False
    is_dates_response = bool(data.get("available_dates"))

    # If this is a dates response, strip time slot data so the frontend
    # doesn't render a time picker at the date selection step.
    if is_dates_response:
        for key in ("available_time_slots", "time_slots", "timeSlots",
                     "available_slots", "timeSlotsGrouped", "slotCount"):
            if key in data:
                del data[key]
                modified = True

    # For pure time-slot responses (no available_dates), add grouping
    if not is_dates_response:
        # LLM may use any of these keys depending on its mood
        slots = (
            data.get("time_slots")
            or data.get("timeSlots")
            or data.get("available_slots")
            or data.get("available_time_slots")
            or []
        )
        if slots and "timeSlotsGrouped" not in data:
            # Build display names from slot objects or strings
            display: list[str] = []
            for s in slots:
                if isinstance(s, dict):
                    display.append(s.get("display_time", s.get("time", "")))
                else:
                    display.append(str(s))

            data["timeSlots"] = display
            data["timeSlotsGrouped"] = _group_time_slots(display)
            data["slotCount"] = len(display)
            modified = True

    if not modified:
        return response_text

    new_json = json.dumps(data, indent=2)
    return response_text[:match.start(1)] + new_json + "\n" + response_text[match.end(1):]


def _detect_response_signals(response_text: str) -> dict:
    """Detect confirmation requests, actions, and PF API errors from response text.

    The LLM includes ``"confirmation_required": true`` in its JSON block
    ONLY when asking the user to confirm a schedule, reschedule, or cancel
    action.  We parse the JSON block rather than regex-matching the natural
    language — the LLM knows what action it is performing.
    """
    signals: dict = {}

    # Parse confirmation_required from the LLM's JSON block.
    # Always present: true for schedule/reschedule/cancel, false otherwise.
    json_match = _JSON_BLOCK_RE.search(response_text)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            confirmed = data.get("confirmation_required", False)
            signals["confirmation_required"] = bool(confirmed)
            if confirmed:
                pending = _build_pending_action(data)
                if pending:
                    signals["pending_action"] = pending
                    signals["action"] = "confirm_appointment_preview"
        except (json.JSONDecodeError, AttributeError):
            signals["confirmation_required"] = False
    else:
        signals["confirmation_required"] = False

    # Detect PF API auth failures in response text
    lower = response_text.lower()
    if "authentication expired" in lower or "please log in again" in lower:
        signals["pf_http_status_code"] = 401

    return signals


def _build_pending_action(json_data: dict) -> dict | None:
    """Build v1.2.9-compatible pending_action from the LLM's JSON block.

    Old format expected by frontend:
        project_name, project_id, project_type, date (display),
        rawDate (YYYY-MM-DD), time (24h), formattedTime (display), address
    """
    raw_date = json_data.get("date", "")
    raw_time = json_data.get("time", "")
    display_time = json_data.get("display_time", json_data.get("formattedTime", ""))
    address = json_data.get("address", json_data.get("installation_address", ""))
    project_type = json_data.get("project_type", "")

    # Split "Windows Installation" into name + type if needed
    project_name = project_type.split(" ")[0] if project_type else ""

    # Format display date from raw date (e.g., "2026-04-18" → "Fri 04/18/2026")
    formatted_date = raw_date
    if raw_date and re.match(r"\d{4}-\d{2}-\d{2}", raw_date):
        try:
            from datetime import datetime

            dt = datetime.strptime(raw_date[:10], "%Y-%m-%d")
            formatted_date = dt.strftime("%a %m/%d/%Y")
        except ValueError:
            pass

    # Build formattedTime from raw time if display_time not provided
    if not display_time and raw_time:
        try:
            from datetime import datetime

            # Handle "13:00:00" or "13:00"
            for fmt in ("%H:%M:%S", "%H:%M"):
                try:
                    dt = datetime.strptime(raw_time, fmt)
                    display_time = dt.strftime("%-I:%M %p")
                    break
                except ValueError:
                    continue
        except Exception:
            display_time = raw_time

    # Extract 24h time for "time" field
    time_24h = raw_time.replace(":00:00", ":00") if raw_time.count(":") == 2 else raw_time

    pending = {
        "project_name": project_name,
        "project_id": json_data.get("project_id", ""),
        "project_type": project_type.split(" ", 1)[1] if " " in project_type else project_type,
        "date": formatted_date,
        "rawDate": raw_date,
        "time": time_24h,
        "formattedTime": display_time,
        "address": address,
    }

    return pending if any(v for v in pending.values()) else None


def _infer_intent(agent_name: str) -> str:
    """Map agent name to v1.2.9-compatible intent string."""
    return _AGENT_TO_INTENT.get(agent_name, "scheduling")


def _build_error_response(
    session_id: str, error_msg: str, pf_status: int | None = None,
) -> JSONResponse:
    """Build an error response matching v1.2.9's error body shape."""
    return JSONResponse(
        status_code=500,
        content={
            "error": error_msg,
            "session_id": session_id,
            "pf_http_status_code": pf_status,
            "agenticscheduler_http_status_code": 500,
        },
    )


async def _store_welcome_in_history(
    session_id: str, user_id: str, response_text: str,
) -> None:
    """Save the welcome response into AgentSquad conversation history.

    This ensures subsequent messages have context about which projects
    were shown to the user during the greeting.
    """
    orchestrator = get_orchestrator()
    try:
        default_agent = orchestrator.default_agent
        if default_agent and orchestrator.storage:
            msg = ConversationMessage(
                role=ParticipantRole.ASSISTANT,
                content=[{"text": response_text}],
            )
            await orchestrator.storage.save_chat_message(
                user_id=user_id,
                session_id=session_id,
                agent_id=default_agent.id,
                new_message=msg,
            )
            logger.info("Welcome response stored in conversation history")
    except Exception:
        logger.exception("Failed to store welcome response in history")


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Send a chat message",
    description=(
        "Send a user message to the scheduling AI agent and receive a complete response.\n\n"
        "**Session continuity:** Include `session_id` from a previous response to continue "
        "the conversation. Omit it to start a new session.\n\n"
        "**Welcome flow:** Send `__WELCOME__` as the message to receive a personalized "
        "greeting with the user's project summary."
    ),
)
async def chat(request: ChatRequest, raw_request: Request):
    """Process a chat message through the orchestrator and return a response."""
    session_id, user_id = _setup_request_context(request, raw_request)
    logger.info("Chat request: message=%s", request.message[:80])

    # Welcome flow — bypass orchestrator
    if request.message.strip() == _WELCOME_TRIGGER:
        try:
            result = await handle_welcome(user_name=request.user_name or "")
            await _store_welcome_in_history(session_id, user_id, result["response"])
            return ChatResponse(
                response=result["response"],
                session_id=session_id,
                agent_name="Welcome",
                intent="welcome",
                action="welcome_with_projects",
                pf_http_status_code=200,
                agenticscheduler_http_status_code=200,
                projects=result.get("projects"),
            )
        except Exception:
            logger.exception("Welcome flow error")
            return _build_error_response(
                session_id, "Failed to process your request. Please try again.",
            )

    orchestrator = get_orchestrator()
    start_time = time.monotonic()
    reset_confirm_flag()
    try:
        response = await orchestrator.route_request(
            user_input=request.message,
            user_id=user_id,
            session_id=session_id,
            additional_params={"channel": "chat"},
        )
    except Exception:
        logger.exception("Orchestrator error")
        return _build_error_response(
            session_id, "Failed to process your request. Please try again.",
        )
    elapsed_ms = int((time.monotonic() - start_time) * 1000)

    response_text = extract_response_text(response.output)

    # Guardrail: detect hallucinated booking confirmations or fabricated failures
    # If the LLM claims a booking was made but confirm_appointment was never called,
    # OR fabricated a "system issue" when the user said "yes", re-route.
    should_retry = False
    if not was_confirm_called() and _looks_like_booking_confirmation(response_text):
        logger.warning("Hallucinated booking detected — retrying with forced tool call")
        should_retry = True
    elif not was_confirm_called() and _looks_like_fabricated_failure(response_text, request.message):
        logger.warning("Fabricated scheduling failure detected — retrying with forced tool call")
        should_retry = True

    if should_retry:
        reset_confirm_flag()
        try:
            response = await orchestrator.route_request(
                user_input=(
                    "The customer confirmed. You MUST call the confirm_appointment tool NOW "
                    "to actually book the appointment. Do NOT respond without calling the tool. "
                    "Do NOT check or worry about the project status — just call the tool."
                ),
                user_id=user_id,
                session_id=session_id,
                additional_params={"channel": "chat"},
            )
            retry_text = extract_response_text(response.output)
            if was_confirm_called():
                response_text = retry_text
                elapsed_ms = int((time.monotonic() - start_time) * 1000)
                logger.info("Retry succeeded — confirm_appointment was called")
            else:
                logger.warning("Retry also failed to call confirm_appointment")
        except Exception:
            logger.exception("Retry failed")

    response_text = _repair_json_blocks(response_text)
    agent_name = response.metadata.agent_name

    logger.info(
        "Chat response: chars=%d agent=%s elapsed_ms=%d",
        len(response_text),
        agent_name,
        elapsed_ms,
    )

    # Enrich JSON block with frontend-required fields (timeSlotsGrouped, etc.)
    response_text = _enrich_json_block(response_text)

    # Detect confirmation requests and PF API errors from response text
    signals = _detect_response_signals(response_text)
    intent = _infer_intent(agent_name)

    # Fire-and-forget conversation log
    task = asyncio.create_task(
        log_conversation(
            session_id=session_id,
            user_id=user_id,
            user_message=request.message,
            bot_response=response_text,
            agent_name=agent_name,
            channel="chat",
            response_time_ms=elapsed_ms,
            intent=intent,
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return ChatResponse(
        response=response_text,
        session_id=session_id,
        agent_name=agent_name,
        intent=intent,
        action=signals.get("action"),
        pf_http_status_code=signals.get("pf_http_status_code", 200),
        agenticscheduler_http_status_code=200,
        confirmation_required=signals.get("confirmation_required"),
        pending_action=signals.get("pending_action"),
    )


@router.post(
    "/chat/stream",
    summary="Send a chat message (streaming)",
    description=(
        "Send a user message and receive the response as a **Server-Sent Events** stream.\n\n"
        "The stream emits two event types in order:\n\n"
        "1. **`delta`** — emitted per incremental text chunk:\n"
        "   ```\n"
        "   event: delta\n"
        '   data: {"text": "Your project is scheduled for "}\n'
        "   ```\n\n"
        "2. **`done`** — emitted once at the end with full metadata:\n"
        "   ```\n"
        "   event: done\n"
        '   data: {"session_id": "abc-123", "agent_name": "...", "intent": "...", '
        '"pf_http_status_code": 200, "agenticscheduler_http_status_code": 200}\n'
        "   ```"
    ),
    responses={
        200: {
            "description": "SSE stream of delta and done events",
            "content": {"text/event-stream": {}},
        },
    },
)
async def chat_stream(request: ChatRequest, raw_request: Request):
    """Stream a chat response as Server-Sent Events (SSE)."""
    session_id, user_id = _setup_request_context(request, raw_request)
    logger.info("Chat stream request: message=%s", request.message[:80])

    # Welcome flow — send as single delta event (no streaming needed for greeting)
    if request.message.strip() == _WELCOME_TRIGGER:
        try:
            result = await handle_welcome(user_name=request.user_name or "")
            await _store_welcome_in_history(session_id, user_id, result["response"])

            async def welcome_stream():
                yield _sse("delta", {"text": result["response"]})
                yield _sse("done", {
                    "session_id": session_id,
                    "agent_name": "Welcome",
                    "intent": "welcome",
                    "action": "welcome_with_projects",
                    "pf_http_status_code": 200,
                    "agenticscheduler_http_status_code": 200,
                    "projects": result.get("projects"),
                })

            return StreamingResponse(welcome_stream(), media_type="text/event-stream")
        except Exception:
            logger.exception("Welcome flow error (stream)")
            return _build_error_response(
                session_id, "Failed to process your request. Please try again.",
            )

    orchestrator = get_orchestrator()
    start_time = time.monotonic()
    try:
        response = await orchestrator.route_request(
            user_input=request.message,
            user_id=user_id,
            session_id=session_id,
            stream_response=True,
            additional_params={"channel": "chat"},
        )
    except Exception:
        logger.exception("Orchestrator error")
        return _build_error_response(
            session_id, "Failed to process your request. Please try again.",
        )

    async def event_stream():
        full_text = ""

        # Non-streaming fallback (e.g. classifier returned a direct answer)
        if not response.streaming:
            text = extract_response_text(response.output)
            full_text = text
            yield _sse("delta", {"text": text})
        else:
            async for chunk in response.output:
                if isinstance(chunk, AgentStreamResponse) and chunk.text:
                    full_text += chunk.text
                    yield _sse("delta", {"text": chunk.text})

        # Repair truncated JSON blocks — LLM may run out of tokens mid-JSON.
        # Send a corrective delta with the closing characters if needed.
        repaired = _repair_json_blocks(full_text)
        if repaired != full_text:
            suffix = repaired[len(full_text):]
            yield _sse("delta", {"text": suffix})
            full_text = repaired

        agent_name = response.metadata.agent_name if hasattr(response, "metadata") else ""
        full_text = _enrich_json_block(full_text)
        signals = _detect_response_signals(full_text)
        intent = _infer_intent(agent_name)

        done_data = {
            "session_id": session_id,
            "agent_name": agent_name,
            "intent": intent,
            "pf_http_status_code": signals.get("pf_http_status_code", 200),
            "agenticscheduler_http_status_code": 200,
        }
        done_data["confirmation_required"] = signals.get("confirmation_required", False)
        if done_data["confirmation_required"]:
            done_data["pending_action"] = signals.get("pending_action")
            done_data["action"] = signals.get("action")

        yield _sse("done", done_data)

        # Fire-and-forget conversation log after stream completes
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        task = asyncio.create_task(
            log_conversation(
                session_id=session_id,
                user_id=user_id,
                user_message=request.message,
                bot_response=full_text,
                agent_name=agent_name,
                channel="chat",
                response_time_ms=elapsed_ms,
                intent=intent,
            )
        )
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _sse(event: str, data: dict) -> str:
    """Format a single SSE frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
