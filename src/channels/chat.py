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
    user_id = request.user_id or "anonymous"

    auth_token = request.auth_token
    if not auth_token:
        auth_header = raw_request.headers.get("authorization", "")
        auth_token = auth_header.removeprefix("Bearer ").strip() if auth_header else ""

    AuthContext.set(
        auth_token=auth_token,
        client_id=request.client_id or "",
        customer_id=request.customer_id or "",
        user_id=user_id,
        user_name=request.user_name or "",
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
]


def _looks_like_booking_confirmation(text: str) -> bool:
    """Check if the response text claims a booking was made."""
    lower = text.lower()
    return any(p in lower for p in _BOOKING_CONFIRMATION_PATTERNS)


def _detect_response_signals(response_text: str) -> dict:
    """Detect confirmation requests, actions, and PF API errors from response text.

    The LLM's response text and tool outputs carry signals that the
    frontend needs as structured fields (v1.2.9 contract).
    """
    signals: dict = {}

    # Detect confirmation-before-write pattern
    # Tool returns "Please confirm: Schedule appointment for project X on Y at Z?"
    if "Please confirm:" in response_text and "confirmed=true" in response_text:
        signals["confirmation_required"] = True
        # Try to extract pending action details from the confirmation prompt
        pending = {}
        # Pattern: "project {id} on {date} at {time}"
        import re
        m = re.search(
            r"project\s+(\S+)\s+on\s+(\S+)\s+at\s+(.+?)(?:\?|\()",
            response_text,
        )
        if m:
            pending = {
                "project_id": m.group(1),
                "date": m.group(2),
                "time": m.group(3).strip(),
            }
        signals["pending_action"] = pending

    # Detect PF API auth failures in response text
    lower = response_text.lower()
    if "authentication expired" in lower or "please log in again" in lower:
        signals["pf_http_status_code"] = 401

    return signals


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

    # Guardrail: detect hallucinated booking confirmations
    # If the LLM claims a booking was made but confirm_appointment was never called,
    # re-route with explicit instruction to call the tool.
    if not was_confirm_called() and _looks_like_booking_confirmation(response_text):
        logger.warning("Hallucinated booking detected — retrying with forced tool call")
        reset_confirm_flag()
        try:
            response = await orchestrator.route_request(
                user_input=(
                    "The customer confirmed. You MUST call the confirm_appointment tool NOW "
                    "to actually book the appointment. Do NOT respond without calling the tool."
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
        action=None,
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

        agent_name = response.metadata.agent_name if hasattr(response, "metadata") else ""
        signals = _detect_response_signals(full_text)
        intent = _infer_intent(agent_name)

        done_data = {
            "session_id": session_id,
            "agent_name": agent_name,
            "intent": intent,
            "pf_http_status_code": signals.get("pf_http_status_code", 200),
            "agenticscheduler_http_status_code": 200,
        }
        if signals.get("confirmation_required"):
            done_data["confirmation_required"] = True
            done_data["pending_action"] = signals.get("pending_action")

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
