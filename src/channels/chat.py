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
from tools.scheduling import (
    reset_action_flags,
    reset_confirm_flag,
    reset_request_caches,
    was_address_updated,
    was_cancel_called,
    was_confirm_called,
    was_time_slots_called,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["chat"])

# Background tasks need a strong reference to avoid GC before completion
_background_tasks: set[asyncio.Task] = set()

_WELCOME_TRIGGER = "__WELCOME__"

# Session-keyed cache of pending confirmation details.
# When the LLM returns confirmation_required=true, we store the appointment
# details here so that when the user clicks Confirm, we can inject context
# telling the LLM exactly which confirm_appointment call to make.
# Keyed by session_id → {"project_id": ..., "date": ..., "time": ...}
_pending_confirmations: dict[str, dict] = {}

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
    "appointment is now scheduled",
    "appointment has been scheduled",
    "appointment has been successfully scheduled",
    "installation is now scheduled",
    "installation has been scheduled",
    "successfully scheduled your",
    "appointment has been booked",
    "you're all set",
    "your appointment is confirmed",
    "booking confirmed",
    "appointment is booked",
    "you're scheduled",
    "have been booked",
    "address has been updated",
    "address updated successfully",
    "address has been changed",
    "installation address has been updated",
]

# Patterns that indicate the LLM fabricated a cancellation without calling the tool
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


def _looks_like_cancel_confirmation(text: str) -> bool:
    """Check if the response text claims a cancellation was done."""
    lower = text.lower()
    return any(p in lower for p in _CANCEL_CONFIRMATION_PATTERNS)


def _looks_like_fabricated_failure(text: str, user_message: str) -> bool:
    """Check if the LLM fabricated a scheduling failure after a user confirmation."""
    if not _AFFIRMATIVE_PATTERNS.match(user_message.strip()):
        return False
    lower = text.lower()
    return any(p in lower for p in _FABRICATED_FAILURE_PATTERNS)


# Regex to detect fabricated time slots: 3+ AM/PM time patterns in a response.
# Only AM/PM — 24h format ("08:00:00") in JSON project data is NOT a fabricated slot.
_TIME_SLOT_PATTERN_AMPM = re.compile(r"\b\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm)\b")

# Scheduling context phrases that indicate the LLM is presenting time slots
# (vs. project data that happens to mention scheduled times).
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
    matches = _TIME_SLOT_PATTERN_AMPM.findall(text)
    if len(matches) < 3:
        return False
    lower = text.lower()
    return any(phrase in lower for phrase in _TIME_SLOT_CONTEXT_PHRASES)


# Pattern to detect if a user message is a date selection
_DATE_SELECTION_PATTERN = re.compile(
    r"(?:"
    r"(?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+\d{1,2}"
    r"|"
    r"\d{1,2}(?:st|nd|rd|th)"
    r"|"
    r"(?:next\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"|"
    r"\d{1,2}[/-]\d{1,2}"
    r")",
    re.IGNORECASE,
)


def _looks_like_date_selection(text: str) -> bool:
    """Check if the user's message looks like they're picking a specific date."""
    return bool(_DATE_SELECTION_PATTERN.search(text))


_ADDRESS_UPDATE_PATTERNS = [
    "address has been noted",
    "address has been saved",
    "address has been updated",
    "address change has been noted",
    "address change has been saved",
    "address update has been noted",
    "address update has been saved",
    "i've noted your address",
    "i've saved your address",
    "i've updated your address",
    "your new address",
    "address will be updated",
    "office will review and update",
]


def _looks_like_address_update(text: str) -> bool:
    """Detect if the response claims an address was saved/noted."""
    lower = text.lower()
    return any(p in lower for p in _ADDRESS_UPDATE_PATTERNS)


_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)


def _strip_json_block_for_confirmation(text: str, signals: dict) -> str:
    """Remove the JSON code block when confirmation_required is true.

    The frontend renders Confirm/Decline buttons from the outer
    ``pending_action`` field — the inline JSON block is redundant and
    displays as raw text in the chat bubble.
    """
    if not signals.get("confirmation_required"):
        return text
    return re.sub(r"\n*```json\s*\n.*?```\s*", "", text, flags=re.DOTALL).strip()


def _strip_markdown_bold(text: str) -> str:
    """Remove markdown bold markers (**text** and __text__) from the natural language
    portion of the response, preserving JSON code blocks untouched."""
    if "**" not in text and "__" not in text:
        return text

    # Split around JSON blocks, only strip markdown in non-JSON parts
    parts = re.split(r"(```json\s*\n.*?```)", text, flags=re.DOTALL)
    for i, part in enumerate(parts):
        if not part.startswith("```json"):
            parts[i] = re.sub(r"\*\*(.+?)\*\*", r"\1", part)
            parts[i] = re.sub(r"__(.+?)__", r"\1", parts[i])
    return "".join(parts)


# Patterns that indicate the LLM is explaining its own mistake to the customer
_SELF_CORRECTION_PATTERNS = [
    r"I apologize for that error.*?(?:\.|$)",
    r"Let me correct my approach.*?(?:\.|$)",
    r"I should (?:ONLY|only|never|always).*?(?:\.|$)",
    r"I must (?:call|always|never).*?(?:\.|$)",
    r"I should never fabricate.*?(?:\.|$)",
    r"Thank you for the correction.*?(?:\.|$)",
    r"You'?re absolutely right.*?(?:\.|$)",
]
_SELF_CORRECTION_RE = re.compile("|".join(_SELF_CORRECTION_PATTERNS), re.IGNORECASE)


def _strip_self_correction(text: str) -> str:
    """Remove LLM self-correction sentences that leak internal guardrail logic.

    These occur when the guardrail retry triggers and the LLM acknowledges
    the correction instead of just providing the right answer.
    """
    cleaned = _SELF_CORRECTION_RE.sub("", text)
    # Collapse whitespace
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r"  +", " ", cleaned)
    return cleaned.strip()


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

    # Strip project_id (internal ID) — only project_number should be visible
    if "project_id" in data:
        del data["project_id"]
        modified = True
    for nested_key in ("project_details", "appointment_details", "appointment", "details"):
        nested = data.get(nested_key)
        if isinstance(nested, dict) and "project_id" in nested:
            del nested["project_id"]
            modified = True

    # Inject full projects list from server-side cache — the LLM truncates large
    # JSON arrays, so the ```json block may have fewer projects than the tool returned.
    if "projects" in data and isinstance(data["projects"], list):
        from tools.scheduling import get_last_projects_list

        cached_projects = get_last_projects_list()
        if cached_projects and len(data["projects"]) < len(cached_projects):
            logger.info(
                "Projects injection: LLM returned %d projects, cache has %d — injecting full list",
                len(data["projects"]),
                len(cached_projects),
            )
            data["projects"] = cached_projects
            data["message"] = f"Found {len(cached_projects)} project(s):"
            modified = True

    is_dates_response = bool(data.get("available_dates"))

    # Convert available_dates from ISO (YYYY-MM-DD) to US format (MM/DD/YYYY)
    # Dates may be strings ("2026-04-24") or dicts ({"date": "2026-04-24", "day": "Friday"})
    if is_dates_response:
        us_dates = []
        for d in data["available_dates"]:
            raw = d["date"] if isinstance(d, dict) else d
            parts = raw.split("-") if isinstance(raw, str) else []
            converted = f"{parts[1]}/{parts[2]}/{parts[0]}" if len(parts) == 3 else raw
            if isinstance(d, dict):
                us_dates.append({**d, "date": converted})
            else:
                us_dates.append(converted)
        data["available_dates"] = us_dates
        modified = True

    # Inject full weather data from server-side cache — the LLM truncates large arrays.
    # Convert compact tuples [date, day, condition, high, indicator] back to dicts
    # for frontend compatibility (frontend reads d.date, d.day_name, d.condition, etc.).
    if is_dates_response:
        from tools.scheduling import get_last_weather_dates

        cached_weather = get_last_weather_dates()
        # Validate cached weather matches the current response's available_dates.
        # Extract ISO dates from cache for comparison (index 0 = date string).
        cached_iso_dates: set[str] = set()
        for entry in cached_weather:
            if isinstance(entry, (list, tuple)) and entry:
                cached_iso_dates.add(entry[0])
            elif isinstance(entry, dict) and "date" in entry:
                cached_iso_dates.add(entry["date"])

        # Convert available_dates back to ISO for comparison (may already be MM/DD/YYYY or dict)
        response_iso_dates: set[str] = set()
        for d in data.get("available_dates", []):
            raw = d["date"] if isinstance(d, dict) else d
            if isinstance(raw, str):
                parts = raw.split("/")
                if len(parts) == 3 and len(parts[2]) == 4:
                    # MM/DD/YYYY → YYYY-MM-DD
                    response_iso_dates.add(f"{parts[2]}-{parts[0]}-{parts[1]}")
                else:
                    response_iso_dates.add(raw)

        # Only inject if there's meaningful overlap (at least 1 shared date)
        weather_matches = bool(cached_iso_dates & response_iso_dates)
        logger.info(
            "Weather injection: is_dates_response=%s, cached_weather_len=%d, "
            "overlap=%s, json_keys=%s",
            is_dates_response, len(cached_weather), weather_matches,
            list(data.keys()),
        )
        if cached_weather and weather_matches:
            enriched = []
            for entry in cached_weather:
                if isinstance(entry, (list, tuple)) and len(entry) >= 5:
                    date_str = entry[0]
                    # Build MM/DD/YYYY display_date from YYYY-MM-DD
                    parts = date_str.split("-") if isinstance(date_str, str) else []
                    display_date = f"{parts[1]}/{parts[2]}/{parts[0]}" if len(parts) == 3 else date_str
                    enriched.append({
                        "date": date_str,
                        "display_date": display_date,
                        "day_name": entry[1],
                        "condition": entry[2],
                        "high_temp": entry[3],
                        "indicator": entry[4],
                    })
                elif isinstance(entry, dict):
                    if "display_date" not in entry and "date" in entry:
                        parts = entry["date"].split("-") if isinstance(entry["date"], str) else []
                        if len(parts) == 3:
                            entry["display_date"] = f"{parts[1]}/{parts[2]}/{parts[0]}"
                    enriched.append(entry)
                else:
                    enriched.append(entry)
            data["dates_with_weather"] = enriched
            logger.info("Weather injection: injected %d dict entries", len(enriched))
            modified = True

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
            or data.get("available_times")
            or data.get("times")
            or data.get("slots")
            or []
        )

        # Server-side injection: replace LLM-fabricated slots with the real
        # ones from get_time_slots.  The LLM often adds hourly business-hour
        # slots on top of the 2 real slots the API returned.
        if slots:
            from tools.scheduling import get_last_time_slots

            cached_slots = get_last_time_slots()
            if cached_slots and len(slots) != len(cached_slots):
                logger.warning(
                    "Time slot injection: LLM has %d slots, API returned %d — replacing",
                    len(slots),
                    len(cached_slots),
                )
                # Normalize keys to time_slots for downstream processing
                for variant in ("time_slots", "timeSlots", "available_slots",
                                "available_time_slots", "available_times", "times", "slots"):
                    data.pop(variant, None)
                data["time_slots"] = cached_slots
                slots = cached_slots
                data["message"] = f"Found {len(cached_slots)} available time slot(s) for {data.get('date', 'the selected date')}."
                modified = True

        if slots and "timeSlotsGrouped" not in data:
            # Build display names from slot objects or strings
            display: list[str] = []
            for s in slots:
                if isinstance(s, dict):
                    display.append(s.get("display_time", s.get("time", "")))
                else:
                    raw = str(s)
                    # Convert HH:MM:SS / HH:MM (24h) to 12h AM/PM display
                    if re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", raw):
                        try:
                            h, m = int(raw.split(":")[0]), int(raw.split(":")[1])
                            suffix = "AM" if h < 12 else "PM"
                            h12 = h % 12 or 12
                            raw = f"{h12}:{m:02d} {suffix}"
                        except (ValueError, IndexError):
                            pass
                    display.append(raw)

            # Remove LLM-variant keys, normalize to timeSlots
            for variant in ("time_slots", "available_slots", "available_time_slots",
                            "available_times", "times", "slots"):
                data.pop(variant, None)
            data["timeSlots"] = display
            data["timeSlotsGrouped"] = _group_time_slots(display)
            data["slotCount"] = len(display)
            modified = True

    # Convert any standalone "date" field from ISO to US format
    raw_date = data.get("date", "")
    if isinstance(raw_date, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", raw_date):
        parts = raw_date.split("-")
        data["date"] = f"{parts[1]}/{parts[2]}/{parts[0]}"
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
                # Extract raw confirm params for session-level injection
                _extract_confirm_params(data, signals)
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
    # LLM may nest fields under various keys depending on phrasing
    d = json_data
    for nested_key in ("appointment_details", "appointment", "details",
                        "project_details", "scheduling_details"):
        if nested_key in d and isinstance(d[nested_key], dict):
            d = {**d, **d[nested_key]}  # merge nested into top level
            break

    raw_date = d.get("date", "") or d.get("scheduled_date", "")
    raw_time = d.get("time", "") or d.get("scheduled_time", "")
    display_time = d.get("display_time", d.get("formattedTime", ""))
    address = d.get("address", d.get("installation_address", ""))
    project_type = d.get("project_type", "")
    category = d.get("category", "")

    # LLM sometimes splits "Windows Installation" into category + project_type
    if category and project_type and category != project_type:
        project_type = f"{category} {project_type}"
    elif not project_type and category:
        project_type = category

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
        "project_number": d.get("project_number", "") or d.get("project_id", ""),
        "project_type": project_type.split(" ", 1)[1] if " " in project_type else project_type,
        "date": formatted_date,
        "rawDate": raw_date,
        "time": time_24h,
        "formattedTime": display_time,
        "address": address,
    }

    return pending if any(v for v in pending.values()) else None


def _extract_confirm_params(json_data: dict, signals: dict) -> None:
    """Extract raw confirm_appointment parameters from the LLM's JSON block.

    These are stored in ``signals["confirm_params"]`` and later cached in
    ``_pending_confirmations`` so the next user message ("Confirm") can
    inject them into the prompt, preventing the LLM from restarting the
    scheduling flow instead of calling confirm_appointment.
    """
    d = json_data
    for nested_key in ("appointment_details", "appointment", "details",
                       "project_details", "scheduling_details"):
        if nested_key in d and isinstance(d[nested_key], dict):
            d = {**d, **d[nested_key]}
            break

    project_id = str(d.get("project_id", "") or d.get("project_number", ""))
    raw_date = d.get("date", "") or d.get("scheduled_date", "")
    raw_time = d.get("time", "") or d.get("scheduled_time", "")

    if project_id and raw_date and raw_time:
        signals["confirm_params"] = {
            "project_id": project_id,
            "date": raw_date,
            "time": raw_time,
        }


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
    reset_action_flags()
    reset_request_caches()

    # If the user is confirming a pending appointment, inject the exact
    # parameters so the LLM calls confirm_appointment instead of restarting
    # the scheduling flow (e.g. calling get_available_dates again).
    user_input = request.message
    pending = _pending_confirmations.get(session_id)
    if pending and _AFFIRMATIVE_PATTERNS.match(user_input.strip()):
        user_input = (
            f"[CONTEXT: The customer confirmed the appointment. "
            f"Call confirm_appointment NOW with project_id={pending['project_id']}, "
            f"date={pending['date']}, time={pending['time']}. "
            f"Do NOT call get_available_dates or any other tool — just confirm.]\n"
            f"{user_input}"
        )
        logger.info("Injected confirm context for session %s", session_id)

    try:
        response = await orchestrator.route_request(
            user_input=user_input,
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
    retry_prompt = ""
    if not was_confirm_called() and _looks_like_booking_confirmation(response_text):
        logger.warning("Hallucinated booking detected — retrying with forced tool call")
        should_retry = True
        retry_prompt = (
            "The customer confirmed. You MUST call the confirm_appointment tool NOW "
            "to actually book the appointment. Do NOT respond without calling the tool. "
            "Do NOT check or worry about the project status — just call the tool."
        )
    elif not was_confirm_called() and _looks_like_fabricated_failure(response_text, request.message):
        logger.warning("Fabricated scheduling failure detected — retrying with forced tool call")
        should_retry = True
        retry_prompt = (
            "The customer confirmed. You MUST call the confirm_appointment tool NOW "
            "to actually book the appointment. Do NOT respond without calling the tool. "
            "Do NOT check or worry about the project status — just call the tool."
        )
    elif not was_cancel_called() and _looks_like_cancel_confirmation(response_text):
        logger.warning("Hallucinated cancellation detected — retrying with forced tool call")
        should_retry = True
        retry_prompt = (
            "You said the appointment was cancelled but you did NOT call the cancel_appointment tool. "
            "The appointment is NOT actually cancelled. You MUST call cancel_appointment(project_id, reason) "
            "NOW to actually cancel it. Use the cancellation reason the customer already provided. "
            "Do NOT respond without calling the tool."
        )
    elif not was_time_slots_called() and _looks_like_time_slot_list(response_text):
        logger.warning("Fabricated time slots detected — retrying with forced tool call")
        should_retry = True
        if _looks_like_date_selection(request.message):
            retry_prompt = (
                f"You fabricated time slots. The customer selected '{request.message}'. "
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
    elif not was_address_updated() and _looks_like_address_update(response_text):
        logger.warning("Hallucinated address update detected — retrying with forced tool call")
        should_retry = True
        retry_prompt = (
            "You told the customer the address was saved/noted but you did NOT call "
            "update_installation_address. The address is NOT saved. "
            "You MUST call update_installation_address NOW with the address details "
            "the customer provided. After that, call add_note starting with "
            "'CUSTOMER REQUESTED INSTALLATION ADDRESS UPDATE'. "
            "Do NOT tell the customer the address is saved until BOTH tools succeed."
        )

    if should_retry:
        # Suppress LLM self-correction text ("I apologize for that error...")
        retry_prompt += (
            " Respond ONLY with the correct answer for the customer. "
            "Do NOT apologize, acknowledge mistakes, or explain what went wrong."
        )
        reset_action_flags()
        try:
            response = await orchestrator.route_request(
                user_input=retry_prompt,
                user_id=user_id,
                session_id=session_id,
                additional_params={"channel": "chat"},
            )
            retry_text = extract_response_text(response.output)
            if was_confirm_called() or was_cancel_called() or was_time_slots_called() or was_address_updated():
                response_text = retry_text
                elapsed_ms = int((time.monotonic() - start_time) * 1000)
                logger.info("Retry succeeded — required tool was called")
            elif not _looks_like_time_slot_list(retry_text):
                response_text = retry_text
                elapsed_ms = int((time.monotonic() - start_time) * 1000)
                logger.info("Retry succeeded — no fabricated slots in response")
            else:
                logger.warning("Retry also fabricated slots")
        except Exception:
            logger.exception("Retry failed")

    response_text = _repair_json_blocks(response_text)
    response_text = _strip_markdown_bold(response_text)
    response_text = _strip_self_correction(response_text)
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

    # Cache or clear pending confirmation for this session
    if signals.get("confirmation_required") and signals.get("confirm_params"):
        _pending_confirmations[session_id] = signals["confirm_params"]
        logger.info("Cached pending confirmation for session %s: %s", session_id, signals["confirm_params"])
    else:
        _pending_confirmations.pop(session_id, None)

    # Strip JSON block for confirmation responses — frontend uses pending_action instead
    response_text = _strip_json_block_for_confirmation(response_text, signals)

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

    # Inject confirm context for streaming endpoint too
    user_input = request.message
    pending = _pending_confirmations.get(session_id)
    if pending and _AFFIRMATIVE_PATTERNS.match(user_input.strip()):
        user_input = (
            f"[CONTEXT: The customer confirmed the appointment. "
            f"Call confirm_appointment NOW with project_id={pending['project_id']}, "
            f"date={pending['date']}, time={pending['time']}. "
            f"Do NOT call get_available_dates or any other tool — just confirm.]\n"
            f"{user_input}"
        )
        logger.info("Injected confirm context for session %s (stream)", session_id)

    try:
        response = await orchestrator.route_request(
            user_input=user_input,
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

        full_text = _strip_markdown_bold(full_text)
        agent_name = response.metadata.agent_name if hasattr(response, "metadata") else ""
        full_text = _enrich_json_block(full_text)
        signals = _detect_response_signals(full_text)
        intent = _infer_intent(agent_name)

        # Cache or clear pending confirmation for this session
        if signals.get("confirmation_required") and signals.get("confirm_params"):
            _pending_confirmations[session_id] = signals["confirm_params"]
        else:
            _pending_confirmations.pop(session_id, None)

        # Strip JSON block for confirmation responses
        display_text = _strip_json_block_for_confirmation(full_text, signals)

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
            # Provide cleaned text without JSON block — frontend can replace streamed content
            done_data["response"] = display_text

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
