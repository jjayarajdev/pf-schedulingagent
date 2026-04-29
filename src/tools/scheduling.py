"""Scheduling tool handlers — 10 async functions for PF Scheduler API.

Ported from v1.2.9 scheduling-actions/handler.py.
All functions are async, use httpx.AsyncClient, return str for the LLM agent.

Architecture: Projects are loaded ONCE per session via the dashboard API and
cached in ``_projects_cache``.  Every tool that needs project data reads from
the cache instead of re-calling the API.  Write operations (schedule, cancel)
invalidate the cache so the next read fetches fresh data.
"""

import asyncio
import json
import logging
import re
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from auth.context import AuthContext
from observability.logging import RequestContext
from tools.api_client import build_headers, get_pf_api_base, log_curl, log_response
from tools.date_utils import convert_natural_date, extract_date_range, normalize_date_str
from tools.project_rules import ProjectStatusRules

logger = logging.getLogger(__name__)

# Scheduler API can be slow (slot computation) — use generous timeout
_SCHEDULER_TIMEOUT = 60.0

# ---------------------------------------------------------------------------
#  Caches (module-level, keyed by customer_id)
# ---------------------------------------------------------------------------

# Projects cache: customer_id → {"projects": [...], "loaded_at": datetime}
_projects_cache: dict[str, dict] = {}

# Cache TTL — 60 seconds: short enough to pick up external changes,
# long enough to avoid redundant calls within a scheduling flow.
_CACHE_TTL_SECONDS = 60

# Cache of request_id per project_id — populated by get_available_dates, used by get_time_slots/confirm
_request_id_by_project: dict[str, int] = {}

# Tracks projects with an active reschedule flow.  Set by _get_reschedule_slots,
# cleared by confirm_appointment.  Prevents the can_schedule("Scheduled") check
# from blocking the confirm call after PF's atomic reschedule-slots endpoint
# already cancelled the old appointment.
_reschedule_pending: set[str] = set()  # project_ids with reschedule in progress

# Cache of old appointment details for recovery if reschedule is incomplete.
# project_id → {"date": "...", "time": "...", "project_type": "...", "project_number": "..."}
_reschedule_old_appointment: dict[str, dict] = {}

# Per-request caches for server-side injection — the LLM truncates large arrays,
# so chat.py injects the full data from these caches.  ContextVar ensures each
# concurrent request gets its own value (no cross-customer data leakage).
_last_weather_dates: ContextVar[list] = ContextVar("last_weather_dates", default=[])
_last_projects_list: ContextVar[list] = ContextVar("last_projects_list", default=[])
_last_time_slots: ContextVar[list] = ContextVar("last_time_slots", default=[])


def get_last_weather_dates() -> list:
    """Return the last weather dates array for server-side injection."""
    return _last_weather_dates.get()


def get_last_projects_list() -> list:
    """Return the last list_projects result for server-side injection."""
    return _last_projects_list.get()


def get_last_time_slots() -> list:
    """Return the last time slots array for server-side injection."""
    return _last_time_slots.get()


def get_reschedule_old_appointment(project_id: str) -> dict | None:
    """Return cached old appointment details for incomplete reschedule detection."""
    return _reschedule_old_appointment.get(project_id)


def clear_reschedule_old_appointment(project_id: str) -> None:
    """Clear cached old appointment after successful reschedule or end-of-call."""
    _reschedule_old_appointment.pop(project_id, None)


def cleanup_call_caches(session_id: str) -> None:
    """Clean up all module-level caches for a completed call.

    Called from the end-of-call handler to free memory and prevent
    stale data from leaking into future calls on the same process.
    """
    # Clear session project tracking
    projects = _session_projects.pop(session_id, {})

    # Clear deferred notes cache
    _session_notes.pop(session_id, None)

    # Clear completed-action tracking
    _session_completed_actions.pop(session_id, None)

    # Clear reschedule state for any projects discussed in this call
    for pid in projects:
        _reschedule_pending.discard(pid)
        _reschedule_old_appointment.pop(pid, None)
        _request_id_by_project.pop(pid, None)

    logger.debug("Cleaned up call caches: session=%s projects=%s", session_id, list(projects.keys()))

# Lock to prevent concurrent API fetches for the same customer
_load_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
#  Per-session project tracking (for end-of-call notes)
# ---------------------------------------------------------------------------

# session_id → {project_id: [action_names]}
_session_projects: dict[str, dict[str, list[str]]] = {}

# Human-readable labels for action names used in end-of-call notes
_ACTION_LABELS: dict[str, str] = {
    "viewed_projects": "Viewed projects",
    "get_project_details": "Viewed project details",
    "get_available_dates": "Checked available dates",
    "get_time_slots": "Checked time slots",
    "confirm_appointment": "Scheduled appointment",
    "reschedule_appointment": "Rescheduled appointment",
    "cancel_appointment": "Cancelled appointment",
    "add_project_note": "Added note",
    "add_customer_note": "Added note",
    "get_installation_address": "Checked installation address",
    "update_installation_address": "Requested address update",
}


def _track_project_action(project_id: str, action: str) -> None:
    """Record that a project was accessed by a tool in the current session."""
    # Track last project for guardrail retry pinning
    if project_id:
        _last_project_id_in_request.set(str(project_id))
    session_id = RequestContext.get_session_id()
    if not session_id:
        return
    if session_id not in _session_projects:
        _session_projects[session_id] = {}
    projects = _session_projects[session_id]
    if project_id not in projects:
        projects[project_id] = []
    if action not in projects[project_id]:
        projects[project_id].append(action)


def track_session_project(session_id: str, project_id: str, action: str = "store_lookup") -> None:
    """Explicitly track a project for a session (used by store auth flow).

    Unlike ``_track_project_action`` which reads session_id from RequestContext,
    this accepts session_id directly — useful when the caller knows the session
    but RequestContext may not be set (e.g., during store authentication).
    """
    if not session_id or not project_id:
        return
    project_id = str(project_id)
    if session_id not in _session_projects:
        _session_projects[session_id] = {}
    projects = _session_projects[session_id]
    if project_id not in projects:
        projects[project_id] = []
    if action not in projects[project_id]:
        projects[project_id].append(action)


def get_session_projects(session_id: str) -> dict[str, list[str]]:
    """Return {project_id: [actions]} for a session. Used by end-of-call handler."""
    return _session_projects.get(session_id, {})


def clear_session_projects(session_id: str) -> None:
    """Remove tracking data for a completed session."""
    _session_projects.pop(session_id, None)


# ---------------------------------------------------------------------------
#  Per-session note cache (deferred posting at end-of-call)
# ---------------------------------------------------------------------------

# session_id → {project_id: [note_text, ...]}
_session_notes: dict[str, dict[str, list[str]]] = {}


def cache_session_note(project_id: str, note_text: str) -> None:
    """Cache a note for deferred posting at end-of-call."""
    session_id = RequestContext.get_session_id()
    if not session_id:
        return
    if session_id not in _session_notes:
        _session_notes[session_id] = {}
    notes = _session_notes[session_id]
    if project_id not in notes:
        notes[project_id] = []
    notes[project_id].append(note_text)


def get_session_notes(session_id: str) -> dict[str, list[str]]:
    """Return {project_id: [note_text, ...]} for a session."""
    return _session_notes.get(session_id, {})


def clear_session_notes(session_id: str) -> None:
    """Remove cached notes for a completed session."""
    _session_notes.pop(session_id, None)


# ---------------------------------------------------------------------------
#  Per-session completed-action tracking (cross-request guardrail)
#
#  Tracks which write actions (confirm, cancel, reschedule) have SUCCEEDED
#  for each project within a call session.  Unlike the per-request ContextVar
#  flags (which reset every `ask_scheduling_bot` invocation), these persist
#  for the entire call.  The guardrail uses them to avoid re-triggering
#  "you MUST call confirm_appointment" when Claude correctly says "your
#  appointment is already confirmed" on a subsequent turn.
# ---------------------------------------------------------------------------

# session_id → {"confirm:PROJECT_ID", "cancel:PROJECT_ID", ...}
_session_completed_actions: dict[str, set[str]] = {}


def mark_session_action(action: str, project_id: str) -> None:
    """Record that a write action succeeded for a project in this session."""
    session_id = RequestContext.get_session_id()
    if not session_id or not project_id:
        return
    if session_id not in _session_completed_actions:
        _session_completed_actions[session_id] = set()
    _session_completed_actions[session_id].add(f"{action}:{project_id}")
    logger.debug(
        "Marked session action: session=%s action=%s project=%s",
        session_id, action, project_id,
    )


def session_action_completed(session_id: str, action: str, project_id: str) -> bool:
    """Check if a write action already succeeded for this project in this session."""
    if not session_id or not project_id:
        return False
    return f"{action}:{project_id}" in _session_completed_actions.get(session_id, set())


def session_has_any_completed(session_id: str, action: str) -> bool:
    """Check if ANY project had this action completed in this session.

    Used when we don't know the specific project_id (e.g. guardrail fires
    before project pinning).
    """
    if not session_id:
        return False
    prefix = f"{action}:"
    return any(k.startswith(prefix) for k in _session_completed_actions.get(session_id, set()))


def clear_session_completed_actions(session_id: str) -> None:
    """Remove completed-action tracking for a finished session."""
    _session_completed_actions.pop(session_id, None)


# ---------------------------------------------------------------------------
#  Request-scoped tool-call tracking (hallucination guardrail)
#
#  Tracks which write operations were ACTUALLY called by the LLM in the
#  current request.  Post-response guardrails in chat.py / vapi.py check
#  these flags to catch hallucinated confirmations, cancellations, etc.
# ---------------------------------------------------------------------------

_confirm_called_in_request: ContextVar[bool] = ContextVar("confirm_called", default=False)
_confirm_success_result: ContextVar[str] = ContextVar("confirm_success_result", default="")
_cancel_called_in_request: ContextVar[bool] = ContextVar("cancel_called", default=False)
_cancel_success_result: ContextVar[str] = ContextVar("cancel_success_result", default="")
_reschedule_called_in_request: ContextVar[bool] = ContextVar("reschedule_called", default=False)
_reschedule_success_result: ContextVar[str] = ContextVar("reschedule_success_result", default="")
_time_slots_called_in_request: ContextVar[bool] = ContextVar("time_slots_called", default=False)
_address_updated_in_request: ContextVar[bool] = ContextVar("address_updated", default=False)
_note_added_in_request: ContextVar[bool] = ContextVar("note_added", default=False)

# Tracks the last project_id used by any tool in this request.
# Used by guardrail retry prompts to pin the project and prevent GPT from
# switching to a different project during the retry.
_last_project_id_in_request: ContextVar[str] = ContextVar("last_project_id", default="")


def reset_confirm_flag() -> None:
    """Reset before each orchestrator call."""
    _confirm_called_in_request.set(False)


def reset_action_flags() -> None:
    """Reset ALL write-action flags before each orchestrator call.

    Note: _last_project_id_in_request is intentionally NOT reset here.
    The guardrail retry needs to read it AFTER the first call fails,
    so it persists across the reset_action_flags() → retry cycle.
    """
    _confirm_called_in_request.set(False)
    _cancel_called_in_request.set(False)
    _reschedule_called_in_request.set(False)
    _time_slots_called_in_request.set(False)
    _address_updated_in_request.set(False)
    _note_added_in_request.set(False)
    # NOTE: _confirm_success_result, _cancel_success_result, and
    # _reschedule_success_result are intentionally NOT reset here.
    # The guardrail retry calls reset_action_flags() before re-invoking
    # the orchestrator.  If the first call already booked/cancelled/rescheduled
    # successfully, we must return the cached result on retry — otherwise the
    # second API call gets a 400 and the LLM tells the customer it failed.


def reset_request_caches() -> None:
    """Reset per-request injection caches before each orchestrator call.

    Prevents stale data from a previous request leaking into the current one.
    Also resets write-action success caches so they don't leak across requests.
    """
    _last_weather_dates.set([])
    _last_projects_list.set([])
    _last_time_slots.set([])
    _confirm_success_result.set("")
    _cancel_success_result.set("")
    _reschedule_success_result.set("")


def was_confirm_called() -> bool:
    """Check if confirm_appointment was actually called in this request."""
    return _confirm_called_in_request.get()


def was_cancel_called() -> bool:
    """Check if cancel_appointment was actually called in this request."""
    return _cancel_called_in_request.get()


def was_reschedule_called() -> bool:
    """Check if reschedule_appointment was actually called in this request."""
    return _reschedule_called_in_request.get()


def was_time_slots_called() -> bool:
    """Check if get_time_slots was actually called in this request."""
    return _time_slots_called_in_request.get()


def was_address_updated() -> bool:
    """Check if update_installation_address was actually called in this request."""
    return _address_updated_in_request.get()


def was_note_added() -> bool:
    """Check if add_note was actually called in this request."""
    return _note_added_in_request.get()


def get_last_project_id() -> str:
    """Return the last project_id used by any tool in this request.

    Used by guardrail retry prompts to pin the correct project so GPT
    cannot drift to a different project during the retry.
    """
    return _last_project_id_in_request.get()


# ---------------------------------------------------------------------------
#  Time normalization
# ---------------------------------------------------------------------------

def _format_time_display(time_str: str) -> str:
    """Convert a 24-hour time string to 12-hour AM/PM for display.

    Accepts: "13:00:00", "08:00:00", "8:00 AM", etc.
    Returns: "1:00 PM", "8:00 AM", etc.
    """
    t = time_str.strip()
    # Already in AM/PM format
    if re.search(r"[AaPp][Mm]", t):
        return t
    # Parse HH:MM or HH:MM:SS
    match = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", t)
    if match:
        hour = int(match.group(1))
        minute = match.group(2)
        period = "AM" if hour < 12 else "PM"
        display_hour = hour if hour <= 12 else hour - 12
        if display_hour == 0:
            display_hour = 12
        return f"{display_hour}:{minute} {period}"
    return t


def _format_date_display(date_str: str) -> str:
    """Convert YYYY-MM-DD to US format MM/DD/YYYY for display.

    Accepts: "2026-04-18"
    Returns: "04/18/2026"
    """
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        return dt.strftime("%m/%d/%Y")
    except (ValueError, TypeError):
        return date_str


def _normalize_time(time_str: str) -> str:
    """Normalize a time string to HH:MM:SS (24-hour) format.

    Accepts: "8:00 AM", "1:00 PM", "08:00", "8:00:00", "13:00:00", etc.
    Returns: "08:00:00", "13:00:00", etc.
    """
    t = time_str.strip()

    # Range format (e.g. "1:00 PM - 3:00 PM") — extract start time only.
    # GPT sometimes fabricates ranges from single start-time slots.
    if " - " in t:
        t = t.split(" - ")[0].strip()

    # Already in HH:MM:SS 24-hour format
    if re.match(r"^\d{2}:\d{2}:\d{2}$", t):
        return t

    # AM/PM format
    am_pm = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)$", t, re.IGNORECASE)
    if am_pm:
        hour = int(am_pm.group(1))
        minute = am_pm.group(2)
        second = am_pm.group(3) or "00"
        period = am_pm.group(4).upper()
        if period == "PM" and hour != 12:
            hour += 12
        elif period == "AM" and hour == 12:
            hour = 0
        return f"{hour:02d}:{minute}:{second}"

    # HH:MM format (no seconds)
    hhmm = re.match(r"^(\d{1,2}):(\d{2})$", t)
    if hhmm:
        return f"{int(hhmm.group(1)):02d}:{hhmm.group(2)}:00"

    # Fallback — return as-is
    logger.warning("Could not normalize time format: %s", time_str)
    return t


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _safe_get(obj: Any, *keys, default=None) -> Any:
    """Safely navigate nested dictionaries."""
    result = obj
    for key in keys:
        if isinstance(result, dict):
            result = result.get(key)
            if result is None:
                return default
        else:
            return default
    return result if result is not None else default


def _extract_project_minimal(item: dict) -> dict[str, Any]:
    """Extract key fields from a raw project API response.

    Store callers only see: status, scheduled date/time, and technician name.
    All other fields (address, category, projectType, store info) are excluded.
    """
    project_type = (
        _safe_get(item, "project_type_project_type", default="")
        or _safe_get(item, "project_type", default="")
        or _safe_get(item, "projectType", default="")
        or _safe_get(item, "work_type", default="")
    )

    is_store = AuthContext.get_caller_type() == "store"

    po_number = _safe_get(item, "project_po_number", default="") or ""
    category = _safe_get(item, "project_category_category", default="")

    project: dict[str, Any] = {
        "id": str(_safe_get(item, "project_project_id", default="")),
        "projectNumber": _safe_get(item, "project_project_number", default=""),
        "status": _safe_get(item, "status_info_status", default=""),
    }
    if po_number:
        project["poNumber"] = po_number

    # Store callers: status, project type, category, scheduled date/time, technician name
    if is_store:
        project["projectType"] = project_type
        if category:
            project["category"] = category

        store_status = project.get("status", "")
        store_has_appt = store_status not in ("Ready To Schedule", "Ready to Schedule")

        scheduled_date = _safe_get(item, "convertedProjectStartScheduledDate")
        if scheduled_date and store_has_appt:
            project["scheduledDate"] = scheduled_date
            project["scheduledEndDate"] = _safe_get(item, "convertedProjectEndScheduledDate", default="")

        installer_name = _safe_get(item, "user_idata_first_name")
        if installer_name and store_has_appt:
            installer_last = _safe_get(item, "user_idata_last_name", default="")
            project["installer"] = {"name": f"{installer_name} {installer_last}".strip()}

        return project

    # Customer callers: full project data
    project["category"] = category
    project["projectType"] = project_type

    # Only include appointment details when the project is actually scheduled.
    # "Ready To Schedule" projects may have stale dates from a previous booking
    # that was cancelled or needs rescheduling — showing those confuses users.
    status = project.get("status", "")
    has_active_appointment = status not in ("Ready To Schedule", "Ready to Schedule")

    scheduled_date = _safe_get(item, "convertedProjectStartScheduledDate")
    if scheduled_date and has_active_appointment:
        project["scheduledDate"] = scheduled_date
        project["scheduledEndDate"] = _safe_get(item, "convertedProjectEndScheduledDate", default="")

    installer_name = _safe_get(item, "user_idata_first_name")
    if installer_name and has_active_appointment:
        installer_last = _safe_get(item, "user_idata_last_name", default="")
        project["installer"] = {
            "name": f"{installer_name} {installer_last}".strip(),
            "id": str(_safe_get(item, "installer_details_installer_id", default="")),
        }

    address = {
        "address_id": _safe_get(item, "project_installation_address_id", default=""),
        "address1": _safe_get(item, "installation_address_address1", default=""),
        "city": _safe_get(item, "installation_address_city", default=""),
        "state": _safe_get(item, "installation_address_state", default=""),
        "zipcode": _safe_get(item, "installation_address_zipcode", default=""),
    }
    project["address"] = {k: v for k, v in address.items() if v}

    # Debug: log address extraction for weather troubleshooting
    if not project["address"]:
        addr_keys = [k for k in item if "address" in k.lower()]
        if addr_keys:
            logger.warning(
                "Project %s has address-like keys but none matched: %s",
                project.get("projectNumber", project["id"]),
                {k: item[k] for k in addr_keys},
            )
        else:
            logger.info(
                "Project %s has no address fields in API response",
                project.get("projectNumber", project["id"]),
            )

    project["store"] = {
        "storeName": _safe_get(item, "store_info_store_name", default=""),
        "storeNumber": _safe_get(item, "store_info_store_number", default=""),
    }

    return project


def _build_scheduler_url(client_id: str, project_id: str) -> str:
    """Build the scheduler API URL path."""
    base = get_pf_api_base()
    return f"{base}/scheduler/client/{client_id}/project/{project_id}"


def _unwrap(data: dict) -> dict:
    """Unwrap the PF API 'data' envelope if present."""
    inner = data.get("data")
    if isinstance(inner, dict):
        return inner
    return data


# ---------------------------------------------------------------------------
#  Project cache operations
# ---------------------------------------------------------------------------

async def _load_projects(force: bool = False) -> list[dict[str, Any]]:
    """Load and cache all projects for the current customer.

    Returns the list of extracted project dicts.  Hits the API only if
    the cache is empty, stale (>60s), or ``force=True``.
    Uses an asyncio.Lock to prevent concurrent API fetches.
    """
    customer_id = AuthContext.get_customer_id()
    if not customer_id:
        return []

    # Fast path: check cache WITHOUT the lock
    if not force and customer_id in _projects_cache:
        entry = _projects_cache[customer_id]
        age = (datetime.now(timezone.utc) - entry["loaded_at"]).total_seconds()
        if age < _CACHE_TTL_SECONDS:
            return entry["projects"]

    # Slow path: acquire lock, re-check, then fetch
    async with _load_lock:
        # Re-check under lock (another coroutine may have populated it)
        if not force and customer_id in _projects_cache:
            entry = _projects_cache[customer_id]
            age = (datetime.now(timezone.utc) - entry["loaded_at"]).total_seconds()
            if age < _CACHE_TTL_SECONDS:
                return entry["projects"]

        # Fetch from API
        client_id = AuthContext.get_client_id()
        url = f"{get_pf_api_base()}/dashboard/get/{client_id}/{customer_id}"
        headers = build_headers()
        log_curl("GET", url, headers)

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.get(url, headers=headers)

            log_response(response, "load_projects")

            if response.status_code in (401, 403):
                return []

            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError:
            logger.exception("Failed to load projects for cache")
            return _projects_cache.get(customer_id, {}).get("projects", [])

        raw_data = data.get("data", [])
        projects = [_extract_project_minimal(item) for item in raw_data]

        _projects_cache[customer_id] = {
            "projects": projects,
            "loaded_at": datetime.now(timezone.utc),
        }
        logger.info("Projects cache loaded: %d projects for customer %s", len(projects), customer_id)
        return projects


def _get_cached_project(project_id: str) -> dict[str, Any] | None:
    """Look up a single project from the cache by ID or project number."""
    customer_id = AuthContext.get_customer_id()
    entry = _projects_cache.get(customer_id)
    if not entry:
        return None

    pid = str(project_id)
    for p in entry["projects"]:
        if p["id"] == pid or p.get("projectNumber") == pid:
            return p
    return None


async def _resolve_project_id(project_id: str) -> str:
    """Resolve a project identifier to the canonical project_id for API calls.

    The LLM may pass either ``project_id`` (e.g. "90000149") or
    ``project_number`` (e.g. "74356_1").  The PF scheduler API requires
    ``project_id``.  This function looks up the cache and returns the
    correct ``project_id`` regardless of which identifier was provided.

    If the cache is empty (e.g. invalidated after a cancel), reloads it
    before giving up — but only when the input looks like a project_number
    (non-numeric), since numeric IDs don't need resolution.
    """
    pid = str(project_id)
    project = _get_cached_project(pid)
    if not project and not pid.isdigit():
        # Non-numeric identifier (project_number) needs resolution.
        # Cache may have been invalidated — reload and retry.
        await _load_projects()
        project = _get_cached_project(pid)
    if project and project["id"] != pid:
        logger.info(
            "Resolved project_number %s → project_id %s",
            project_id,
            project["id"],
        )
        return project["id"]
    return pid


def _invalidate_projects() -> None:
    """Invalidate the project cache for the current customer.

    Called after write operations (schedule, cancel) so the next read
    picks up the updated status.
    """
    customer_id = AuthContext.get_customer_id()
    if customer_id in _projects_cache:
        del _projects_cache[customer_id]
        logger.info("Projects cache invalidated for customer %s", customer_id)


# ---------------------------------------------------------------------------
#  Tool handlers
# ---------------------------------------------------------------------------

_SCHEDULED_STATUSES = {
    "scheduled", "tentatively scheduled", "customer scheduled",
    "store scheduled", "install scheduled", "hdms scheduled",
}
_CANCELLED_STATUSES = {"cancelled", "cancelled/surge", "ready to cancel"}
_COMPLETED_STATUSES = {
    "completed", "work complete", "project completed",
    "work order completed", "done", "completed-archived",
}
_ON_HOLD_STATUSES = {
    "on hold", "waiting for product", "waiting product",
    "missing product", "paused - missing product",
    "paused - waiting on product", "waiting for permit",
    "needs permit", "product ordered", "pending",
}

# Combined terminal statuses — excluded from list_projects by default
_TERMINAL_STATUSES = _COMPLETED_STATUSES | _CANCELLED_STATUSES | {"closed", "archived"}


def _build_intelligent_fallback(all_projects: list[dict[str, Any]]) -> str:
    """When no schedulable projects remain, explain WHY with context.

    Categorizes projects by status and returns a helpful message with
    actionable next steps.
    """
    scheduled = []
    cancelled = []
    completed = []
    on_hold = []
    other = []

    for p in all_projects:
        s = p.get("status", "").lower()
        if s in _SCHEDULED_STATUSES:
            scheduled.append(p)
        elif s in _CANCELLED_STATUSES:
            cancelled.append(p)
        elif s in _COMPLETED_STATUSES:
            completed.append(p)
        elif s in _ON_HOLD_STATUSES:
            on_hold.append(p)
        else:
            other.append(p)

    # Priority order: already scheduled → cancelled → completed → on hold → generic
    if scheduled:
        items = [
            f"{p.get('category', 'Project')} (#{p['id']})"
            + (f" scheduled for {p['scheduledDate']}" if p.get("scheduledDate") else "")
            for p in scheduled
        ]
        info = "; ".join(items)
        result = {
            "message": (
                f"I found {len(scheduled)} project(s), but they're already scheduled: {info}. "
                "Would you like to reschedule, cancel, or check the details?"
            ),
            "projects": scheduled,
            "already_scheduled": True,
        }
        return json.dumps(result, indent=2)

    if cancelled:
        cat = cancelled[0].get("category", "project")
        return (
            f"Your {cat} project has been cancelled and cannot be scheduled. "
            "Would you like more information or to speak with customer service?"
        )

    if completed:
        cat = completed[0].get("category", "project")
        return (
            f"Your {cat} project has been completed. "
            "Is there anything else I can help you with?"
        )

    if on_hold:
        p = on_hold[0]
        actual_status = p.get("status", "on hold")
        cat = p.get("category", "project")
        return (
            f"Your {cat} project is currently {actual_status} and cannot be scheduled yet. "
            "Would you like more details?"
        )

    # Generic — show what we have
    items = [
        f"{p.get('category', 'Project')} (#{p['id']}): {p.get('status', 'Unknown')}"
        for p in all_projects[:5]
    ]
    info = "; ".join(items)
    return (
        f"I found {len(all_projects)} project(s): {info}. "
        "None are ready to schedule right now. Would you like more details?"
    )


async def list_projects(
    status: str = "", category: str = "", project_type: str = "",
    scheduled_month: str = "", scheduled_date: str = "",
) -> str:
    """List projects for the authenticated customer.

    Supports filtering by status, category, projectType, scheduled_month,
    and scheduled_date. A special ``status="schedulable"`` filter matches
    projects in schedulable statuses (new, ready to schedule, etc.).
    """
    customer_id = AuthContext.get_customer_id()
    if not customer_id:
        return "Error: No customer ID available. Please ensure you're authenticated."

    projects = await _load_projects()

    if not projects:
        return "No projects found."

    # Exclude terminal statuses by default (unless a specific status filter asks for them)
    if status and status.lower() in _TERMINAL_STATUSES:
        # If user explicitly asks for a terminal status, don't exclude it
        active_projects = list(projects)
    else:
        active_projects = [p for p in projects if p.get("status", "").lower() not in _TERMINAL_STATUSES]

    filtered = list(active_projects)

    # Apply filters
    if status:
        status_lower = status.lower()
        if status_lower == "schedulable":
            schedulable = {"new", "pending reschedule", "not scheduled", "ready to schedule", "ready for auto call"}
            filtered = [p for p in filtered if p.get("status", "").lower() in schedulable]
        else:
            filtered = [p for p in filtered if status_lower in p.get("status", "").lower()]

    if category:
        cat_lower = category.lower()
        filtered = [p for p in filtered if cat_lower in p.get("category", "").lower()]

    if project_type:
        pt_lower = project_type.lower()
        filtered = [
            p for p in filtered
            if pt_lower in (p.get("projectType") or "").lower()
        ]

    if scheduled_month:
        month_lower = scheduled_month.lower()[:3]  # "January" → "jan"
        filtered = [
            p for p in filtered
            if _match_scheduled_month(p.get("scheduledDate", ""), month_lower)
        ]

    if scheduled_date:
        filtered = [
            p for p in filtered
            if _match_scheduled_date(p.get("scheduledDate", ""), scheduled_date)
        ]

    if not filtered:
        # Intelligent fallback — only reference active projects, never completed/cancelled
        return _build_intelligent_fallback(active_projects)

    # Track all returned projects so end-of-call notes get posted for every caller type.
    for p in filtered:
        pid = str(p.get("id", ""))
        if pid:
            _track_project_action(pid, "viewed_projects")

    # Cache for server-side injection — the LLM truncates large JSON arrays
    _last_projects_list.set(filtered)

    return json.dumps({"message": f"Found {len(filtered)} project(s):", "projects": filtered}, indent=2)


def _match_scheduled_month(scheduled_date: str, month_abbrev: str) -> bool:
    """Check if a scheduledDate falls in a given month (e.g., 'jan', 'feb')."""
    if not scheduled_date:
        return False
    try:
        dt = datetime.strptime(scheduled_date[:10], "%Y-%m-%d")
        return dt.strftime("%b").lower() == month_abbrev
    except ValueError:
        return False


def _match_scheduled_date(scheduled_date: str, target_date: str) -> bool:
    """Check if a scheduledDate matches a target date (YYYY-MM-DD)."""
    if not scheduled_date:
        return False
    return scheduled_date[:10] == target_date[:10]


def _annotate_day_names(dates: list[str]) -> list[dict[str, str]]:
    """Convert date strings to dicts with the correct day name.

    ["2026-04-24"] → [{"date": "2026-04-24", "day": "Friday"}]

    This prevents the LLM from hallucinating incorrect day-of-week names.
    """
    result = []
    for d in dates:
        try:
            dt = datetime.strptime(d[:10], "%Y-%m-%d")
            result.append({"date": d[:10], "day": dt.strftime("%A")})
        except (ValueError, TypeError):
            result.append({"date": d, "day": ""})
    return result


def _get_project_number(project_id: str) -> str:
    """Return the user-facing project number for a project_id.

    Falls back to ``project_id`` if the cache miss or no projectNumber.
    """
    project = _get_cached_project(project_id)
    if project:
        return project.get("projectNumber") or project_id
    return project_id


async def get_project_details(project_id: str) -> str:
    """Get detailed information about a specific project.

    Reads from the project cache.  If not found, forces a cache refresh
    and retries once.
    """
    project_id = await _resolve_project_id(project_id)
    _track_project_action(project_id, "get_project_details")
    customer_id = AuthContext.get_customer_id()
    if not customer_id:
        return "Error: No customer ID available. Please ensure you're authenticated."

    # Try cache first
    project = _get_cached_project(project_id)
    if not project:
        # Cache miss — load fresh and retry
        await _load_projects(force=True)
        project = _get_cached_project(project_id)

    if not project:
        pnum = _get_project_number(project_id)
        return f"Project {pnum} not found. Please check the project number and try again."

    result = {
        "project": project,
        "message": f"Project #{project.get('projectNumber') or project.get('id', '')} Details",
    }
    return json.dumps(result, indent=2, default=str)


async def get_available_dates(project_id: str, start_date: str = "", end_date: str = "") -> str:
    """Get available scheduling dates. Returns dates + request_id (critical for downstream calls)."""
    project_id = await _resolve_project_id(project_id)
    _track_project_action(project_id, "get_available_dates")
    client_id = AuthContext.get_client_id()
    base_url = _build_scheduler_url(client_id, project_id)

    # Normalize natural language dates to YYYY-MM-DD (e.g. "April 1, 2026" → "2026-04-01")
    # Must happen before all other date logic so clamping/parsing works on ISO format.
    start_date = normalize_date_str(start_date)
    end_date = normalize_date_str(end_date)

    if start_date and not end_date:
        range_result = extract_date_range(start_date)
        if range_result:
            start_date = range_result["start_date"]
            end_date = range_result["end_date"]
        else:
            date_result = convert_natural_date(start_date)
            if date_result and date_result.get("past"):
                pnum = _get_project_number(project_id)
                return json.dumps({
                    "project_number": pnum,
                    "available_dates": [],
                    "past_dates": True,
                    "message": (
                        f"The requested dates ({date_result['start_date']} to "
                        f"{date_result['end_date']}) have already passed. "
                        "Please pick a future date range, or I can show you "
                        "the next available dates."
                    ),
                }, indent=2)
            if date_result:
                start_date = date_result["start_date"]
                end_date = date_result.get("end_date", "")

    # Reject past dates — clamp to tomorrow
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)
    if start_date:
        try:
            start_dt_check = datetime.strptime(start_date[:10], "%Y-%m-%d").date()
            if start_dt_check < today:
                logger.info("Past date requested (%s), clamping to tomorrow (%s)", start_date, tomorrow)
                start_date = tomorrow.strftime("%Y-%m-%d")
                end_date = ""  # Reset so it recalculates from new start
        except ValueError:
            pass

    if not start_date:
        start_date = tomorrow.strftime("%Y-%m-%d")
    if not end_date:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_date = (start_dt + timedelta(days=10)).strftime("%Y-%m-%d")

    headers = build_headers()

    url = f"{base_url}/startDate/{start_date}/endDate/{end_date}/slotsChatbot"
    log_curl("GET", url, headers)
    try:
        async with httpx.AsyncClient(timeout=_SCHEDULER_TIMEOUT) as client:
            response = await client.get(url, headers=headers)
        log_response(response, "get_available_dates")

        # Check for "already scheduled" errors BEFORE raise_for_status
        if response.status_code == 400:
            error_body = response.text.lower()
            if any(phrase in error_body for phrase in (
                "already requested", "already scheduled",
                "already contains a technician", "technician already assigned",
                "not allowed",
            )):
                logger.info("Project %s is already scheduled (API 400)", project_id)
                result = {
                    "project_number": _get_project_number(project_id),
                    "already_scheduled": True,
                    "available_dates": [],
                    "message": (
                        "This project is already scheduled. "
                        "Would you like to reschedule to a different date?"
                    ),
                }
                return json.dumps(result, indent=2)

        response.raise_for_status()
        data = _unwrap(response.json())
    except httpx.HTTPError as exc:
        resp = getattr(exc, "response", None)
        logger.exception(
            "Failed to get available dates: project=%s url=%s status=%s body=%s",
            project_id, url,
            resp.status_code if resp is not None else "N/A",
            resp.text[:500] if resp is not None and resp.text else "N/A",
        )
        return "Sorry, I couldn't check available dates right now. Please try again later."

    dates = data.get("dates", data.get("availableDates", []))
    api_request_id = data.get("request_id")

    if not dates:
        expanded_end = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=21)).strftime("%Y-%m-%d")
        url = f"{base_url}/startDate/{start_date}/endDate/{expanded_end}/slotsChatbot"
        try:
            async with httpx.AsyncClient(timeout=_SCHEDULER_TIMEOUT) as client:
                response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = _unwrap(response.json())
            dates = data.get("dates", data.get("availableDates", []))
            api_request_id = data.get("request_id", api_request_id)
        except httpx.HTTPError:
            logger.exception("Failed on expanded date search")

    if not dates:
        project = _get_cached_project(project_id)
        logger.warning(
            "No dates returned after retry: project=%s project_type=%s url=%s response_keys=%s",
            project_id,
            project.get("projectType", "unknown") if project else "unknown",
            url,
            list(data.keys()) if isinstance(data, dict) else type(data).__name__,
        )
        return f"No available dates found between {start_date} and {end_date}. Try a different date range."

    # Weather enrichment for outdoor projects
    weather_dates = None
    project = _get_cached_project(project_id)
    if project:
        category = project.get("category", "") or project.get("projectType", "")
        from tools.weather_aware import enrich_dates_with_weather, is_outdoor_project

        if is_outdoor_project(project.get("category", ""), project.get("projectType", "")):
            try:
                weather_dates = await enrich_dates_with_weather(dates, category, project)
            except Exception:
                logger.exception("Weather enrichment failed (non-fatal)")

    # Sort dates chronologically
    sorted_dates = sorted(dates)

    # Annotate each date with its day name so the LLM doesn't hallucinate it
    dated_with_days = _annotate_day_names(sorted_dates)

    # IMPORTANT: Never include time slots in the dates response.
    # The slotsChatbot endpoint may return generic slots alongside dates,
    # but including them causes the LLM to fabricate additional 30-min
    # increments between the real slots.  The correct flow is:
    #   1. get_available_dates → returns ONLY dates
    #   2. User picks a date
    #   3. get_time_slots(date) → returns actual slots for that date
    pnum = _get_project_number(project_id)
    result: dict[str, Any] = {
        "project_number": pnum,
        "available_dates": dated_with_days,
        "date_range": {"start": start_date, "end": end_date},
        "message": f"Found {len(sorted_dates)} available date(s). Which date works best for you?",
        "_llm_instruction": (
            "Once the customer picks a date, you MUST call get_time_slots "
            "to get the actual available time slots. Do NOT guess, fabricate, "
            "or infer time slots — ONLY use what get_time_slots returns."
        ),
    }

    if api_request_id:
        # Cache internally — do NOT expose in response, the LLM confuses
        # it with project_id and passes it to subsequent tool calls.
        _request_id_by_project[project_id] = api_request_id

    if weather_dates:
        # Sort weather dates chronologically too
        # date is index 0 in [date, day, condition, high, indicator]
        sorted_weather = sorted(weather_dates, key=lambda d: d[0])
        result["dates_with_weather"] = sorted_weather
        _last_weather_dates.set(sorted_weather)
        # indicator is index 4 in [date, day, condition, high, indicator]
        good = sum(1 for d in weather_dates if d[4] != "[BAD]")
        bad = len(weather_dates) - good
        result["message"] += (
            f" Weather checked for {project.get('category', 'outdoor')} work: "
            f"{good} good day(s), {bad} day(s) with concerns."
        )
    else:
        _last_weather_dates.set([])

    return json.dumps(result, indent=2)


async def get_time_slots(project_id: str, date: str) -> str:
    """Get available time slots for a specific date."""
    _time_slots_called_in_request.set(True)
    project_id = await _resolve_project_id(project_id)
    _track_project_action(project_id, "get_time_slots")
    # Normalize natural language dates (e.g. "May 5" → "2026-05-05")
    date = normalize_date_str(date)
    # Fix common LLM date errors: wrong year
    try:
        date_obj = datetime.strptime(date, "%Y-%m-%d")
        current_year = datetime.now().year
        if date_obj.year < current_year:
            date = date.replace(str(date_obj.year), str(current_year), 1)
            logger.warning("Corrected date year from %d to %d: %s", date_obj.year, current_year, date)
            date_obj = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        pass

    # Reject past dates
    today = datetime.now().date()
    try:
        if datetime.strptime(date, "%Y-%m-%d").date() < today:
            tomorrow = (today + timedelta(days=1)).strftime("%Y-%m-%d")
            logger.info("Past date requested for time slots (%s), rejecting", date)
            return json.dumps({
                "project_number": _get_project_number(project_id),
                "error": "past_date",
                "requested_date": _format_date_display(date),
                "message": (
                    f"That date ({_format_date_display(date)}) has already passed. "
                    f"Scheduling is only available from {_format_date_display(tomorrow)} onwards. "
                    "Would you like me to check the next available dates?"
                ),
            }, indent=2)
    except ValueError:
        pass

    client_id = AuthContext.get_client_id()
    base_url = _build_scheduler_url(client_id, project_id)
    headers = build_headers()

    # Get request_id from cache (populated by get_available_dates)
    request_id = _request_id_by_project.get(project_id)
    if not request_id:
        logger.warning("No cached request_id for project %s — calling get_available_dates first", project_id)
        await get_available_dates(project_id)
        request_id = _request_id_by_project.get(project_id)
    if not request_id:
        pnum = _get_project_number(project_id)
        return f"Could not get request_id for project {pnum}. Please call get_available_dates first."

    url = f"{base_url}/startDate/{date}/endDate/{date}/slotsChatbot"
    params = {"request_id": str(request_id)}
    log_curl("GET", url, headers)
    try:
        async with httpx.AsyncClient(timeout=_SCHEDULER_TIMEOUT) as client:
            response = await client.get(url, headers=headers, params=params)
        log_response(response, "get_time_slots")
        response.raise_for_status()
        data = _unwrap(response.json())
    except httpx.HTTPError as exc:
        logger.exception("Failed to get time slots")
        return f"Sorry, I couldn't fetch time slots for {_format_date_display(date)}. Please try again later."

    slots = data.get("slots", data.get("timeSlots", []))
    if not slots:
        return f"No time slots available for {_format_date_display(date)}. Please try another date."

    # Cache request_id internally for confirm_appointment — do NOT expose
    # to the LLM (it confuses request_id with project_id).
    api_rid = data.get("request_id", request_id)
    if api_rid:
        _request_id_by_project[project_id] = api_rid
    # Format slots as human-readable start times so GPT/Claude relay exact values
    # and don't fabricate ranges. Raw 24h values ("13:00:00") cause GPT to guess
    # windows like "1 to 3 PM" which then fail confirm_appointment.
    display_slots = [_format_time_display(s) for s in slots]

    # Cache display-format slots for server-side injection — prevents the LLM
    # from fabricating additional slots beyond what the API returned.
    _last_time_slots.set(display_slots)

    pnum = _get_project_number(project_id)
    date_display = _format_date_display(date)
    result = {
        "project_number": pnum,
        "date": date_display,
        "time_slots": display_slots,
        "message": f"Found {len(slots)} available time slot(s) for {date_display}.",
    }
    return json.dumps(result, indent=2)


async def confirm_appointment(project_id: str, date: str, time: str, **kwargs) -> str:
    """Schedule an appointment. Only call AFTER the customer has confirmed."""
    _confirm_called_in_request.set(True)
    # Guard against duplicate confirm calls in the same request — the LLM
    # sometimes calls confirm_appointment twice in a single tool-use loop.
    # The second call hits a 400 "Invalid process step" and the LLM then
    # tells the customer "already scheduled, want to reschedule?" instead
    # of showing the success message.
    cached = _confirm_success_result.get()
    if cached:
        logger.warning("confirm_appointment called again in same request — returning cached success")
        return cached
    try:
        return await _confirm_appointment_impl(project_id, date, time, **kwargs)
    except (httpx.HTTPError, json.JSONDecodeError):
        raise  # Let known errors propagate for specific handling
    except Exception:
        logger.exception("Unexpected error in confirm_appointment (project=%s)", project_id)
        return "Sorry, something went wrong while scheduling. Please try again."


async def _confirm_appointment_impl(project_id: str, date: str, time: str, **kwargs) -> str:
    """Internal implementation of confirm_appointment."""
    project_id = await _resolve_project_id(project_id)
    _track_project_action(project_id, "confirm_appointment")

    # Validate status from cache (no extra API call).
    # Skip when a reschedule is pending — PF's atomic reschedule-slots endpoint
    # already cancelled the old appointment, but the cached status may still show
    # "Scheduled" if the project cache was reloaded between the reschedule and confirm.
    is_reschedule = project_id in _reschedule_pending
    project = _get_cached_project(project_id)
    if project and not is_reschedule:
        status = project.get("status", "")
        if status:
            can_schedule, reason = ProjectStatusRules.can_schedule(status)
            if not can_schedule:
                return reason

    # Use the request_id from cache (populated by get_available_dates)
    request_id = _request_id_by_project.get(project_id, 0)
    normalized_time = _normalize_time(time)

    # Normalize date — GPT often sends natural language or wrong year (e.g. 2024)
    date = normalize_date_str(date)
    try:
        date_obj = datetime.strptime(date, "%Y-%m-%d")
        current_year = datetime.now().year
        if date_obj.year < current_year:
            date = date.replace(str(date_obj.year), str(current_year), 1)
            logger.warning("confirm_appointment: corrected date year from %d to %d: %s", date_obj.year, current_year, date)
    except ValueError:
        pass

    client_id = AuthContext.get_client_id()
    base_url = _build_scheduler_url(client_id, project_id)
    url = f"{base_url}/schedule"
    headers = build_headers()
    # Match v1.2.9 payload exactly: snake_case field names, integer request_id
    payload = {
        "created_at": datetime.now(timezone.utc).strftime("%m/%d/%Y %H:%M:%S"),
        "date": date,
        "time": normalized_time,
        "request_id": request_id,
        "is_chatbot": "true",
        "aIBot": True,
    }
    log_curl("POST", url, headers, payload)

    try:
        async with httpx.AsyncClient(timeout=_SCHEDULER_TIMEOUT) as client:
            response = await client.post(url, headers=headers, json=payload)
        log_response(response, "confirm_appointment")
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        logger.exception("Failed to confirm appointment")
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code == 400:
            return "Invalid appointment details. The time slot may no longer be available. Please check available dates again."
        if status_code == 409:
            return "This time slot has already been booked or there's a conflict. Please choose a different time."
        if status_code == 401:
            return "Authentication expired. Please log in again."
        return "Sorry, I couldn't schedule the appointment. Please try again later."

    # Log the full response for debugging
    logger.info("Confirm appointment response body: %s", json.dumps(data, default=str))

    # Validate the response — PF API may return 200 with error in body
    if isinstance(data, dict):
        # Check for error indicators in response
        error_msg = data.get("error") or data.get("message", "")
        if data.get("error"):
            logger.error("Confirm API returned error in body: %s", error_msg)
            return f"Scheduling failed: {error_msg}"

    # Invalidate project cache — status will change after scheduling
    _invalidate_projects()
    _reschedule_pending.discard(project_id)

    # Extract confirmation details (API may return data as True or a dict)
    confirmation_details = data.get("data", {})
    if not isinstance(confirmation_details, dict):
        confirmation_details = {}

    confirmation_number = (
        confirmation_details.get("confirmation_number", "")
        or data.get("confirmationNumber", "")
        or data.get("confirmation_number", "")
    )

    date_display = _format_date_display(date)
    time_display = _format_time_display(normalized_time)

    # Check if the PF API says the appointment needs office review
    api_message = data.get("message", "")
    is_pending_review = "review" in api_message.lower() or "request" in api_message.lower()

    if is_pending_review:
        msg = (
            f"Your scheduling request for {date_display} at {time_display} has been submitted. "
            f"The office will review and confirm it shortly. "
            f"The project status is NOT yet 'Scheduled' — it will update once the office approves. "
            f"Do NOT tell the customer it is already scheduled."
        )
    else:
        msg = f"Appointment confirmed! Your appointment is scheduled for {date_display} at {time_display}."
    if confirmation_number:
        msg += f" Confirmation number: {confirmation_number}."
    if not is_pending_review:
        msg += " You will receive a confirmation to your registered email and phone number."
    _confirm_success_result.set(msg)
    mark_session_action("confirm", project_id)
    return msg


async def reschedule_appointment(project_id: str) -> str:
    """Start reschedule flow — cancels existing appointment and fetches new dates.

    Uses ``cancel-reschedule`` to cancel the current appointment, then the
    standard ``slotsChatbot`` endpoint for new date/slot availability.
    """
    _reschedule_called_in_request.set(True)
    cached = _reschedule_success_result.get()
    if cached:
        logger.warning("reschedule_appointment called again in same request — returning cached success")
        return cached
    try:
        return await _reschedule_appointment_impl(project_id)
    except Exception:
        logger.exception("Unexpected error in reschedule_appointment (project=%s)", project_id)
        return "Sorry, something went wrong during rescheduling. Please try again."


async def _reschedule_appointment_impl(project_id: str) -> str:
    """Internal implementation of reschedule_appointment."""
    project_id = await _resolve_project_id(project_id)
    _track_project_action(project_id, "reschedule_appointment")
    # Read from cache (no extra API call)
    project = _get_cached_project(project_id)
    if not project:
        await _load_projects(force=True)
        project = _get_cached_project(project_id)

    if not project:
        pnum = _get_project_number(project_id)
        return f"Project {pnum} not found. Please check the project number."

    status = project.get("status", "")
    has_date = bool(project.get("scheduledDate"))

    if status:
        can_reschedule, reason = ProjectStatusRules.can_reschedule(status, has_date)
        if not can_reschedule:
            return reason

    # Cache old appointment details for recovery if reschedule flow is incomplete
    old_date = project.get("scheduledDate", "")
    old_time = project.get("scheduledTime", "")
    project_type = project.get("projectType", project.get("category", ""))
    if old_date:
        _reschedule_old_appointment[project_id] = {
            "date": old_date,
            "time": old_time,
            "project_type": project_type,
            "project_number": project.get("projectNumber", project_id),
        }
        logger.info(
            "Cached old appointment before reschedule: project=%s date=%s time=%s",
            project_id, old_date, old_time,
        )

    # PF's reschedule endpoint handles cancel + new slots atomically.
    # Do NOT call cancel_appointment separately — if the customer declines
    # the new slots, the project stays in "reschedule pending" state instead
    # of being hard-cancelled.
    reschedule_dates = await _get_reschedule_slots(project_id)
    if reschedule_dates:
        _invalidate_projects()
        _reschedule_pending.add(project_id)
        _reschedule_success_result.set(reschedule_dates)
        mark_session_action("reschedule", project_id)
        return reschedule_dates

    # _get_reschedule_slots may have successfully cancelled but returned no
    # dates (API returns only a message).  If it set _reschedule_pending,
    # guide the LLM to fetch dates via the normal flow.
    if project_id in _reschedule_pending:
        msg = (
            "The existing appointment has been cancelled. "
            "Now call get_available_dates to fetch available dates for rescheduling."
        )
        _reschedule_success_result.set(msg)
        mark_session_action("reschedule", project_id)
        return msg

    return (
        "Couldn't fetch reschedule availability. "
        "Please try again or use get_available_dates to see available scheduling dates."
    )


async def _get_reschedule_slots(project_id: str) -> str | None:
    """Cancel existing appointment and fetch available reschedule dates.

    Uses ``GET /cancel-reschedule`` which cancels the current
    appointment and returns available dates for rescheduling.

    Returns a JSON response string on success, or ``None`` to fall back
    to the standard ``get_available_dates`` flow.
    """
    client_id = AuthContext.get_client_id()
    base_url = _build_scheduler_url(client_id, project_id)
    url = f"{base_url}/cancel-reschedule"
    headers = build_headers()
    log_curl("GET", url, headers)

    try:
        async with httpx.AsyncClient(timeout=_SCHEDULER_TIMEOUT) as client:
            response = await client.get(url, headers=headers)
        log_response(response, "cancel_reschedule")

        if response.status_code != 200:
            error_text = response.text.lower()
            if "project status should be" in error_text:
                logger.warning("Rescheduler API rejected project %s — status mismatch", project_id)
                return None
            logger.warning("Rescheduler API returned %d for project %s", response.status_code, project_id)
            return None

        data = _unwrap(response.json())
    except httpx.HTTPError:
        logger.exception("Rescheduler API failed for project %s — falling back", project_id)
        return None

    dates = data.get("dates", data.get("availableDates", []))

    # Capture request_id so downstream get_time_slots doesn't need to call
    # get_available_dates again (which might fail post-reschedule).
    api_rid = data.get("request_id")
    if api_rid:
        _request_id_by_project[project_id] = api_rid

    if not dates:
        # The cancel-reschedule API may return only a success message without
        # dates.  Mark the reschedule as pending so confirm_appointment skips
        # the "already scheduled" check, and tell the caller to fetch dates
        # via the normal get_available_dates flow.
        _reschedule_pending.add(project_id)
        _invalidate_projects()
        return None

    # Weather enrichment for outdoor projects
    weather_dates = None
    project = _get_cached_project(project_id)
    if project:
        from tools.weather_aware import enrich_dates_with_weather, is_outdoor_project

        if is_outdoor_project(project.get("category", ""), project.get("projectType", "")):
            try:
                category = project.get("category", "") or project.get("projectType", "")
                weather_dates = await enrich_dates_with_weather(dates, category, project)
            except Exception:
                logger.exception("Weather enrichment failed during reschedule (non-fatal)")

    # IMPORTANT: Do NOT include time slots from the cancel-reschedule API.
    # The slots returned are generic per-day slots, not date-specific availability.
    # Including them causes the LLM to present fabricated slots before the customer
    # picks a date.  The correct flow is: dates → customer picks → get_time_slots.

    pnum = _get_project_number(project_id)
    result: dict[str, Any] = {
        "project_number": pnum,
        "is_reschedule": True,
        "available_dates": _annotate_day_names(dates),
        "message": (
            f"The existing appointment has been cancelled. "
            f"Found {len(dates)} new date(s) for rescheduling."
            " Present these dates to the customer. Once they pick a date,"
            " you MUST call get_time_slots to get the actual available time slots."
            " Do NOT guess, fabricate, or infer time slots."
        ),
    }

    if weather_dates:
        result["dates_with_weather"] = weather_dates
        # indicator is index 4 in [date, day, condition, high, indicator]
        good = sum(1 for d in weather_dates if d[4] != "[BAD]")
        bad = len(weather_dates) - good

    return json.dumps(result, indent=2)


async def cancel_appointment(project_id: str, reason: str = "") -> str:
    """Cancel an existing appointment.

    Args:
        project_id: The project ID or project number.
        reason: The cancellation reason (required by business rules).
            If provided, a cancellation note is automatically saved.
    """
    _cancel_called_in_request.set(True)
    cached = _cancel_success_result.get()
    if cached:
        logger.warning("cancel_appointment called again in same request — returning cached success")
        return cached
    try:
        return await _cancel_appointment_impl(project_id, reason)
    except Exception:
        logger.exception("Unexpected error in cancel_appointment (project=%s)", project_id)
        return "Sorry, something went wrong while cancelling. Please try again."


async def _cancel_appointment_impl(project_id: str, reason: str = "") -> str:
    """Internal implementation of cancel_appointment."""
    project_id = await _resolve_project_id(project_id)
    _track_project_action(project_id, "cancel_appointment")
    # Validate from cache (no extra API call)
    project = _get_cached_project(project_id)
    if project:
        status = project.get("status", "")
        has_date = bool(project.get("scheduledDate"))
        if status:
            can_cancel, validation_msg = ProjectStatusRules.can_cancel(status, has_date)
            if not can_cancel:
                return validation_msg

    client_id = AuthContext.get_client_id()
    base = get_pf_api_base()
    url = f"{base}/scheduler/{client_id}/{project_id}/cancel-appointment"
    headers = build_headers()
    log_curl("GET", url, headers)

    try:
        async with httpx.AsyncClient(timeout=_SCHEDULER_TIMEOUT) as client:
            response = await client.get(url, headers=headers)
        log_response(response, "cancel_appointment")
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.exception("Failed to cancel appointment")
        return "Sorry, I couldn't cancel the appointment. Please try again later."

    # Invalidate project cache — status changed
    _invalidate_projects()
    _reschedule_pending.discard(project_id)
    mark_session_action("cancel", project_id)

    # Auto-save cancellation reason note if provided
    if reason.strip():
        note_text = f"CANCELLATION REASON: {reason.strip()}. Cancelled via AI Scheduling Assistant."
        try:
            note_result = await add_note(project_id, note_text)
            logger.info("Cancel note saved for project %s: %s", project_id, note_result)
            if "sorry" in note_result.lower() or "couldn't" in note_result.lower():
                msg = (
                    "Appointment has been cancelled successfully. "
                    "However, I was unable to save the cancellation reason note. "
                    "Please try adding the note manually."
                )
                _cancel_success_result.set(msg)
                return msg
        except Exception:
            logger.exception("Failed to save cancellation note for project %s", project_id)
            msg = (
                "Appointment has been cancelled successfully. "
                "However, I was unable to save the cancellation reason note. "
                "Please try adding the note manually."
            )
            _cancel_success_result.set(msg)
            return msg

    msg = "Appointment has been cancelled successfully."
    _cancel_success_result.set(msg)
    return msg


async def add_note(project_id: str, note_text: str) -> str:
    """Add a note to a project. Routes to the correct API based on caller type and note content."""
    _note_added_in_request.set(True)
    project_id = await _resolve_project_id(project_id)
    caller_type = AuthContext.get_caller_type()

    is_cancel_note = any(
        prefix in note_text.upper()
        for prefix in ("CANCELLATION REASON:", "CANCELLATION:", "RESCHEDULE REASON:")
    )
    is_address_note = "CUSTOMER REQUESTED INSTALLATION ADDRESS UPDATE" in note_text.upper()

    if is_address_note:
        return await _add_address_note(project_id, note_text)
    elif caller_type == "store" or is_cancel_note:
        return await _add_project_note(project_id, note_text)
    else:
        return await _add_customer_note(project_id, note_text)


async def _add_address_note(project_id: str, note_text: str) -> str:
    """Post an address update note via POST /project-notes/update-address-note.

    Used for: customer address update requests.
    """
    _track_project_action(project_id, "add_address_note")
    client_id = AuthContext.get_client_id()
    base = get_pf_api_base()
    headers = build_headers()

    try:
        pid = int(project_id)
    except (ValueError, TypeError):
        pid = project_id

    url = f"{base}/project-notes/update-address-note"
    payload = {
        "client_id": client_id,
        "project_id": pid,
        "note_text": note_text,
    }

    logger.info("add_address_note (project-notes/update-address-note) for project %s", project_id)
    log_curl("POST", url, headers, payload)

    try:
        async with httpx.AsyncClient(timeout=_SCHEDULER_TIMEOUT) as client:
            response = await client.post(url, headers=headers, json=payload)
        log_response(response, "add_address_note")
        response.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Failed to add address note")
        return "Sorry, I couldn't add the note. Please try again later."

    pnum = _get_project_number(project_id)
    return f"Address update note added successfully to project {pnum}."


async def _add_project_note(project_id: str, note_text: str) -> str:
    """Post a note via POST /project-notes/add-note.

    Used for: store callers, cancel/reschedule reasons, address update requests.
    """
    _track_project_action(project_id, "add_project_note")
    client_id = AuthContext.get_client_id()
    base = get_pf_api_base()
    headers = build_headers()

    try:
        pid = int(project_id)
    except (ValueError, TypeError):
        pid = project_id

    url = f"{base}/project-notes/add-note"
    payload = {
        "client_id": client_id,
        "project_id": pid,
        "note_text": note_text,
    }

    logger.info("add_project_note (project-notes/add-note) for project %s", project_id)
    log_curl("POST", url, headers, payload)

    try:
        async with httpx.AsyncClient(timeout=_SCHEDULER_TIMEOUT) as client:
            response = await client.post(url, headers=headers, json=payload)
        log_response(response, "add_project_note")
        response.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Failed to add project note")
        return "Sorry, I couldn't add the note. Please try again later."

    pnum = _get_project_number(project_id)
    return f"Note added successfully to project {pnum}."


async def _add_customer_note(project_id: str, note_text: str) -> str:
    """Add a customer note — cached for phone calls, posted immediately for chat.

    Phone (vapi): notes are collected during the call and posted in bulk at
    end-of-call via POST /customer-project-notes/add-customer-notes/.
    Chat: notes are posted immediately via POST /communication/.../note.
    """
    _track_project_action(project_id, "add_customer_note")
    channel = RequestContext.get_channel()

    # Phone calls: cache for deferred posting at end-of-call
    if channel == "vapi":
        cache_session_note(project_id, note_text)
        pnum = _get_project_number(project_id)
        logger.info("Customer note cached for project %s (deferred to end-of-call)", project_id)
        return f"Note added successfully to project {pnum}."

    # Chat/other channels: post immediately to /communication/.../note
    client_id = AuthContext.get_client_id()
    url = f"{get_pf_api_base()}/communication/client/{client_id}/project/{project_id}/note"
    headers = build_headers()
    payload = {"note_text": note_text}
    log_curl("POST", url, headers, payload)

    try:
        async with httpx.AsyncClient(timeout=_SCHEDULER_TIMEOUT) as client:
            response = await client.post(url, headers=headers, json=payload)
        log_response(response, "add_customer_note")
        response.raise_for_status()
    except httpx.HTTPError:
        logger.exception("Failed to add customer note for project %s", project_id)
        return "Sorry, I couldn't add the note. Please try again later."

    pnum = _get_project_number(project_id)
    return f"Note added successfully to project {pnum}."


async def list_notes(project_id: str) -> str:
    """List notes for a project."""
    project_id = await _resolve_project_id(project_id)
    client_id = AuthContext.get_client_id()
    customer_id = AuthContext.get_customer_id()
    url = f"{get_pf_api_base()}/communication/client/{client_id}/customer/{customer_id}/project/{project_id}/notes"
    headers = build_headers()
    log_curl("GET", url, headers)

    try:
        async with httpx.AsyncClient(timeout=_SCHEDULER_TIMEOUT) as client:
            response = await client.get(url, headers=headers)
        log_response(response, "list_notes")
        response.raise_for_status()
        data = response.json()
    except httpx.HTTPError as exc:
        logger.exception("Failed to list notes")
        return "Sorry, I couldn't fetch notes. Please try again later."

    notes = data if isinstance(data, list) else data.get("notes", data.get("data", []))
    if not notes:
        pnum = _get_project_number(project_id)
        return f"No notes found for project {pnum}."

    return json.dumps({"notes": notes, "count": len(notes)}, indent=2, default=str)


async def post_call_summary_notes(
    *,
    session_id: str,
    bearer_token: str,
    client_id: str,
    customer_id: str,
    summary: str,
    duration_seconds: float = 0,
    projects_discussed: dict[str, list[str]] | None = None,
    cached_notes: dict[str, list[str]] | None = None,
) -> None:
    """Post call summary as a note to each project discussed during the session.

    Called from the Vapi end-of-call handler. Uses explicit auth params since
    AuthContext is not available outside the request lifecycle.

    Args:
        projects_discussed: Pre-extracted {project_id: [actions]} from the session.
            Must be passed by the caller before cleanup_call_caches() clears the data.
        cached_notes: Pre-extracted deferred notes from the session.
    """
    if projects_discussed is None:
        projects_discussed = get_session_projects(session_id)
    if not projects_discussed:
        logger.info("No projects discussed in session %s — skipping call notes", session_id)
        return

    # Format duration
    minutes = int(duration_seconds // 60)
    seconds = int(duration_seconds % 60)
    duration_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

    call_time = datetime.now(timezone.utc).strftime("%b %d, %Y at %I:%M %p UTC")
    base_url = get_pf_api_base()
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "client_id": client_id,
    }

    # Collect cached notes for deferred posting (use pre-extracted if available)
    if cached_notes is None:
        cached_notes = get_session_notes(session_id)

    async with httpx.AsyncClient(timeout=15.0) as client:
        for project_id, actions in projects_discussed.items():
            # ── 1. Conversation summary → /communication/.../note ──
            note_text = (
                f"Customer called on {call_time} via AI Scheduling Assistant (J). "
                f"Duration: {duration_str}."
            )
            if summary:
                note_text += f" {summary}"

            url = f"{base_url}/communication/client/{client_id}/project/{project_id}/note"
            viewed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            payload = {
                "note_text": f"{note_text}\n\n— AI Scheduling Assistant (J)",
                "viewed_by": "6fb91b7b-f5bf-11ec-8fa1-0a924d8c6a19",
                "reviewed_by": "6fb91b7b-f5bf-11ec-8fa1-0a924d8c6a19",
                "viewed_at": viewed_at,
                "reviewed_at": viewed_at,
            }
            try:
                log_curl("POST", url, headers, payload)
                resp = await client.post(url, headers=headers, json=payload)
                log_response(resp, "post_call_summary_notes (communication/note)")
                logger.info(
                    "Call summary posted: project=%s status=%d session=%s",
                    project_id, resp.status_code, session_id,
                )
            except Exception:
                logger.exception(
                    "Failed to post call summary for project %s (session=%s)",
                    project_id, session_id,
                )

            # ── 2. Project notes → /project-notes/add/{client_id}/{project_id} ──
            project_notes = cached_notes.get(project_id, [])
            if project_notes:
                combined_notes = "\n".join(project_notes)
                notes_url = f"{base_url}/customer-project-notes/add-customer-notes/{client_id}/{project_id}"
                notes_payload = {"note_text": combined_notes}
                try:
                    log_curl("POST", notes_url, headers, notes_payload)
                    resp = await client.post(notes_url, headers=headers, json=notes_payload)
                    log_response(resp, "post_call_project_notes (project-notes/add)")
                    logger.info(
                        "Project notes posted: project=%s notes=%d status=%d session=%s",
                        project_id, len(project_notes), resp.status_code, session_id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to post project notes for project %s (session=%s)",
                        project_id, session_id,
                    )

    # Note: cleanup is handled by the caller (cleanup_call_caches) —
    # do NOT call clear_session_projects/clear_session_notes here.


async def post_store_call_notes(
    *,
    session_id: str,
    bearer_token: str,
    client_id: str,
    summary: str,
    duration_seconds: float = 0,
    projects_discussed: dict[str, list[str]] | None = None,
) -> None:
    """Post call summary as a note for store calls via /project-notes/add-note.

    Store callers don't have customer-level auth, so we use the separate
    add-note endpoint that only requires client_id + project_id.
    Called from the Vapi end-of-call handler for store sessions.

    Args:
        projects_discussed: Pre-extracted {project_id: [actions]} from the session.
            Must be passed by the caller before cleanup_call_caches() clears the data.
    """
    if projects_discussed is None:
        projects_discussed = get_session_projects(session_id)
    if not projects_discussed:
        logger.warning("No projects discussed in store session %s — skipping call notes", session_id)
        return

    minutes = int(duration_seconds // 60)
    seconds = int(duration_seconds % 60)
    duration_str = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"

    call_time = datetime.now(timezone.utc).strftime("%b %d, %Y at %I:%M %p UTC")
    base_url = get_pf_api_base()
    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        for project_id, actions in projects_discussed.items():
            note_text = (
                f"Store called on {call_time} via AI Scheduling Assistant (J). "
                f"Duration: {duration_str}."
            )
            if summary:
                note_text += f" {summary}"

            url = f"{base_url}/project-notes/add-note"
            payload = {
                "client_id": client_id,
                "project_id": int(project_id),
                "note_text": note_text,
            }
            try:
                log_curl("POST", url, headers, payload)
                resp = await client.post(url, headers=headers, json=payload)
                log_response(resp, "post_store_call_notes (project-notes/add-note)")
                logger.info(
                    "Store call note posted: project=%s status=%d session=%s",
                    project_id, resp.status_code, session_id,
                )
            except Exception:
                logger.exception(
                    "Failed to post store call note for project %s (session=%s)",
                    project_id, session_id,
                )

    # Note: cleanup is handled by the caller (cleanup_call_caches).


async def get_business_hours() -> str:
    """Get business hours for the client."""
    client_id = AuthContext.get_client_id()
    url = f"{get_pf_api_base()}/scheduler/client/{client_id}/business-hours"
    headers = build_headers()
    log_curl("GET", url, headers)

    try:
        async with httpx.AsyncClient(timeout=_SCHEDULER_TIMEOUT) as client:
            response = await client.get(url, headers=headers)
        log_response(response, "get_business_hours")
        response.raise_for_status()
        data = _unwrap(response.json())
    except httpx.HTTPError as exc:
        logger.exception("Failed to get business hours")
        return "Sorry, I couldn't fetch business hours. Please try again later."

    return json.dumps(data, indent=2, default=str)


async def get_project_weather(project_id: str = "") -> str:
    """Get weather forecast for a project's installation address.

    If ``project_id`` is provided, looks up that project's address.
    Otherwise uses the first project with an address from the cache.
    """
    from tools.weather import get_weather

    projects: list[dict[str, Any]] = []
    customer_id = AuthContext.get_customer_id()
    entry = _projects_cache.get(customer_id)
    if entry:
        projects = entry["projects"]
    else:
        projects = await _load_projects()

    if not projects:
        return "No projects found. Please list your projects first."

    # Find the target project
    target = None
    if project_id:
        pid = str(project_id)
        for p in projects:
            if p["id"] == pid or p.get("projectNumber") == pid:
                target = p
                break
        if not target:
            return f"Project {project_id} not found."
    else:
        # Prefer a scheduled project (user likely wants weather for their appointment)
        for p in projects:
            addr = p.get("address", {})
            if addr.get("city") and p.get("scheduledDate"):
                target = p
                break
        # Fallback to any project with an address
        if not target:
            for p in projects:
                addr = p.get("address", {})
                if addr.get("city"):
                    target = p
                    break

    if not target:
        return "No project address available. Please specify a location."

    addr = target.get("address", {})
    city = addr.get("city", "")
    state = addr.get("state", "")
    zipcode = addr.get("zipcode", "")

    if not city:
        return f"Project {target['id']} has no address on file. Please provide a location."

    parts = [city]
    if state:
        parts.append(state)
    if zipcode:
        parts.append(zipcode)
    location = ", ".join(parts)

    # Pass scheduled date so weather focuses on the installation day
    scheduled_date = target.get("scheduledDate", "")

    logger.info(
        "Weather for project %s at %s (scheduled=%s)",
        target["id"], location, scheduled_date or "unscheduled",
    )
    result = await get_weather(location, target_date=scheduled_date)

    # Merge project context into weather JSON
    try:
        weather_data = json.loads(result)
        project_label = target.get("category", target.get("projectType", "Project"))
        weather_data["project"] = project_label
        weather_data["project_number"] = target.get("projectNumber") or target["id"]
        if not scheduled_date:
            weather_data["message"] = f"Weather for {project_label} at {location}"
        return json.dumps(weather_data, indent=2)
    except (json.JSONDecodeError, TypeError):
        # Fallback for non-JSON weather responses (error messages)
        return result


# ---------------------------------------------------------------------------
#  Installation address tools
# ---------------------------------------------------------------------------

async def get_installation_address(project_id: str, **kwargs) -> str:
    """Get the installation address for a project.

    Returns address details including ``address_id`` (needed for updates).
    Currently uses cached address from the dashboard API.

    TODO: uncomment API call when /authentication/get-installation-address is ready.
    """
    project_id = await _resolve_project_id(project_id)
    _track_project_action(project_id, "get_installation_address")

    # --- API call commented out for now ---
    # client_id = AuthContext.get_client_id()
    # base_url = get_pf_api_base()
    # url = f"{base_url}/authentication/get-installation-address"
    # headers = build_headers()
    # body = {"client_id": client_id, "project_ids": [int(project_id)]}
    #
    # try:
    #     async with httpx.AsyncClient(timeout=_SCHEDULER_TIMEOUT) as client:
    #         log_curl("POST", url, headers, body)
    #         resp = await client.post(url, headers=headers, json=body)
    #         log_response(resp, "get-installation-address")
    #         resp.raise_for_status()
    #
    #         data = resp.json()
    #         # API returns list of addresses; find the one for our project
    #         addresses = data if isinstance(data, list) else data.get("data", [data])
    #         addr_entry = None
    #         for entry in addresses:
    #             if str(entry.get("project_id", "")) == str(project_id):
    #                 addr_entry = entry
    #                 break
    #         if not addr_entry and addresses:
    #             addr_entry = addresses[0]
    #
    #         if addr_entry:
    #             # API nests address fields under an "address" key
    #             addr_data = addr_entry.get("address", addr_entry)
    #             address = {
    #                 "address_id": str(
    #                     addr_data.get("address_id", "")
    #                     or addr_data.get("id", "")
    #                     or addr_entry.get("id", "")
    #                 ),
    #                 "address1": addr_data.get("address1", ""),
    #                 "city": addr_data.get("city", ""),
    #                 "state": addr_data.get("state", ""),
    #                 "zipcode": addr_data.get("zipcode", ""),
    #             }
    #             # Cache address_id on the project for subsequent update calls
    #             cached = _get_cached_project(project_id)
    #             if cached and cached.get("address"):
    #                 cached["address"]["address_id"] = address["address_id"]
    #
    #             return json.dumps({
    #                 "project_id": project_id,
    #                 "address": {k: v for k, v in address.items() if v},
    #                 "message": f"Installation address for project {project_id}",
    #             })
    #
    #         # No matching address in response — fall through to cache
    #         logger.warning("No address in API response for project %s", project_id)
    #
    # except Exception:
    #     logger.exception("get-installation-address API failed for project %s", project_id)

    # Use cached address from dashboard
    cached = _get_cached_project(project_id)
    if cached and cached.get("address"):
        pnum = cached.get("projectNumber") or project_id
        return json.dumps({
            "project_number": pnum,
            "address": cached["address"],
            "message": f"Installation address for project {pnum}",
        })

    pnum = _get_project_number(project_id)
    return f"Could not retrieve the installation address for project {pnum}."


async def update_installation_address(
    project_id: str,
    address1: str,
    city: str,
    state: str = "",
    zipcode: str = "",
    **kwargs,
) -> str:
    """Capture an address change request as a project note.

    Direct address updates are not supported — all address change requests
    are saved as notes for the office to review and process manually.
    """
    _address_updated_in_request.set(True)
    project_id = await _resolve_project_id(project_id)
    _track_project_action(project_id, "update_installation_address")
    pnum = _get_project_number(project_id)

    address_parts = [v for v in [address1, city, state, zipcode] if v]
    note_text = (
        f"CUSTOMER REQUESTED INSTALLATION ADDRESS UPDATE. "
        f"New address: {', '.join(address_parts)}."
    )

    try:
        await add_note(project_id, note_text)
        mark_session_action("address_update", project_id)
        return (
            f"I've noted your address change request for project {pnum}. "
            "The office will review and update it."
        )
    except Exception:
        logger.exception("Failed to save address update note for project %s", project_id)
        return (
            f"I wasn't able to save the address change request for project {pnum} right now. "
            "Please contact the office directly to request the change."
        )
