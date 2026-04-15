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
from tools.date_utils import convert_natural_date, extract_date_range
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

# Per-request caches for server-side injection — the LLM truncates large arrays,
# so chat.py injects the full data from these caches.  ContextVar ensures each
# concurrent request gets its own value (no cross-customer data leakage).
_last_weather_dates: ContextVar[list] = ContextVar("last_weather_dates", default=[])
_last_projects_list: ContextVar[list] = ContextVar("last_projects_list", default=[])


def get_last_weather_dates() -> list:
    """Return the last weather dates array for server-side injection."""
    return _last_weather_dates.get()


def get_last_projects_list() -> list:
    """Return the last list_projects result for server-side injection."""
    return _last_projects_list.get()

# Lock to prevent concurrent API fetches for the same customer
_load_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
#  Per-session project tracking (for end-of-call notes)
# ---------------------------------------------------------------------------

# session_id → {project_id: [action_names]}
_session_projects: dict[str, dict[str, list[str]]] = {}


def _track_project_action(project_id: str, action: str) -> None:
    """Record that a project was accessed by a tool in the current session."""
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


def get_session_projects(session_id: str) -> dict[str, list[str]]:
    """Return {project_id: [actions]} for a session. Used by end-of-call handler."""
    return _session_projects.get(session_id, {})


def clear_session_projects(session_id: str) -> None:
    """Remove tracking data for a completed session."""
    _session_projects.pop(session_id, None)


# ---------------------------------------------------------------------------
#  Request-scoped tool-call tracking (hallucination guardrail)
#
#  Tracks which write operations were ACTUALLY called by the LLM in the
#  current request.  Post-response guardrails in chat.py / vapi.py check
#  these flags to catch hallucinated confirmations, cancellations, etc.
# ---------------------------------------------------------------------------

_confirm_called_in_request: ContextVar[bool] = ContextVar("confirm_called", default=False)
_cancel_called_in_request: ContextVar[bool] = ContextVar("cancel_called", default=False)
_reschedule_called_in_request: ContextVar[bool] = ContextVar("reschedule_called", default=False)


def reset_confirm_flag() -> None:
    """Reset before each orchestrator call."""
    _confirm_called_in_request.set(False)


def reset_action_flags() -> None:
    """Reset ALL write-action flags before each orchestrator call."""
    _confirm_called_in_request.set(False)
    _cancel_called_in_request.set(False)
    _reschedule_called_in_request.set(False)


def reset_request_caches() -> None:
    """Reset per-request injection caches before each orchestrator call.

    Prevents stale data from a previous request leaking into the current one.
    """
    _last_weather_dates.set([])
    _last_projects_list.set([])


def was_confirm_called() -> bool:
    """Check if confirm_appointment was actually called in this request."""
    return _confirm_called_in_request.get()


def was_cancel_called() -> bool:
    """Check if cancel_appointment was actually called in this request."""
    return _cancel_called_in_request.get()


def was_reschedule_called() -> bool:
    """Check if reschedule_appointment was actually called in this request."""
    return _reschedule_called_in_request.get()


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

    project: dict[str, Any] = {
        "id": str(_safe_get(item, "project_project_id", default="")),
        "projectNumber": _safe_get(item, "project_project_number", default=""),
        "status": _safe_get(item, "status_info_status", default=""),
    }

    # Store callers: only status, scheduled date/time, technician name
    if is_store:
        scheduled_date = _safe_get(item, "convertedProjectStartScheduledDate")
        if scheduled_date:
            project["scheduledDate"] = scheduled_date
            project["scheduledEndDate"] = _safe_get(item, "convertedProjectEndScheduledDate", default="")

        installer_name = _safe_get(item, "user_idata_first_name")
        if installer_name:
            installer_last = _safe_get(item, "user_idata_last_name", default="")
            project["installer"] = {"name": f"{installer_name} {installer_last}".strip()}

        return project

    # Customer callers: full project data
    project["category"] = _safe_get(item, "project_category_category", default="")
    project["projectType"] = project_type

    scheduled_date = _safe_get(item, "convertedProjectStartScheduledDate")
    if scheduled_date:
        project["scheduledDate"] = scheduled_date
        project["scheduledEndDate"] = _safe_get(item, "convertedProjectEndScheduledDate", default="")

    installer_name = _safe_get(item, "user_idata_first_name")
    if installer_name:
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


def _resolve_project_id(project_id: str) -> str:
    """Resolve a project identifier to the canonical project_id for API calls.

    The LLM may pass either ``project_id`` (e.g. "90000149") or
    ``project_number`` (e.g. "74356_1").  The PF scheduler API requires
    ``project_id``.  This function looks up the cache and returns the
    correct ``project_id`` regardless of which identifier was provided.
    """
    project = _get_cached_project(project_id)
    if project and project["id"] != str(project_id):
        logger.info(
            "Resolved project_number %s → project_id %s",
            project_id,
            project["id"],
        )
        return project["id"]
    return str(project_id)


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
            schedulable = {"new", "pending reschedule", "not scheduled", "ready to schedule"}
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

    # Store callers: track all returned projects so end-of-call notes get posted.
    # Customer callers: only track via specific tool calls (get_project_details, etc.)
    if AuthContext.get_caller_type() == "store":
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
    project_id = _resolve_project_id(project_id)
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
    project_id = _resolve_project_id(project_id)
    _track_project_action(project_id, "get_available_dates")
    client_id = AuthContext.get_client_id()
    base_url = _build_scheduler_url(client_id, project_id)

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

    # Use reschedule-specific endpoint when a reschedule is in progress
    if project_id in _reschedule_pending:
        url = f"{base_url}/date/{start_date}/selected/{start_date}/get-reschedule-slotsChatBot"
        log_curl("GET", url, headers)
        try:
            async with httpx.AsyncClient(timeout=_SCHEDULER_TIMEOUT) as client:
                response = await client.get(url, headers=headers)
            log_response(response, "get_reschedule_available_dates")
            response.raise_for_status()
            data = _unwrap(response.json())
        except httpx.HTTPError as exc:
            logger.exception("Failed to get reschedule available dates")
            return f"Sorry, I couldn't check available dates for rescheduling. Error: {exc}"
    else:
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
            logger.exception("Failed to get available dates")
            return f"Sorry, I couldn't check available dates. Error: {exc}"

    dates = data.get("dates", data.get("availableDates", []))
    api_request_id = data.get("request_id")

    if not dates:
        expanded_end = (datetime.strptime(start_date, "%Y-%m-%d") + timedelta(days=21)).strftime("%Y-%m-%d")
        if project_id in _reschedule_pending:
            url = f"{base_url}/date/{start_date}/selected/{start_date}/get-reschedule-slotsChatBot"
        else:
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

    # Include available time slots from the API response.
    # The slotsChatbot endpoint returns the valid time slots alongside dates.
    # Including them here prevents the LLM from fabricating slots.
    raw_slots = data.get("slots", [])
    formatted_slots = []
    for s in raw_slots:
        try:
            h, m, *_ = s.split(":")
            hour = int(h)
            minute = m
            period = "AM" if hour < 12 else "PM"
            display_hour = hour if hour <= 12 else hour - 12
            if display_hour == 0:
                display_hour = 12
            formatted_slots.append(f"{display_hour}:{minute} {period}")
        except (ValueError, IndexError):
            formatted_slots.append(s)

    pnum = _get_project_number(project_id)
    result: dict[str, Any] = {
        "project_number": pnum,
        "available_dates": sorted_dates,
        "date_range": {"start": start_date, "end": end_date},
        "message": f"Found {len(sorted_dates)} available date(s).",
    }

    if formatted_slots:
        result["available_time_slots"] = formatted_slots
        result["message"] += (
            f" Available time slots on each date: {', '.join(formatted_slots)}."
        )
    else:
        result["available_time_slots"] = []
        result["message"] += (
            " IMPORTANT: No time slots were returned with dates."
            " You MUST call get_time_slots with the customer's chosen date"
            " to get the actual available time slots. Do NOT guess or fabricate time slots."
        )

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
    project_id = _resolve_project_id(project_id)
    _track_project_action(project_id, "get_time_slots")
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

    # Use reschedule-specific endpoint when a reschedule is in progress,
    # otherwise use the standard slotsChatbot endpoint.
    if project_id in _reschedule_pending:
        url = f"{base_url}/date/{date}/selected/{date}/get-reschedule-slotsChatBot"
        log_curl("GET", url, headers)
        try:
            async with httpx.AsyncClient(timeout=_SCHEDULER_TIMEOUT) as client:
                response = await client.get(url, headers=headers)
            log_response(response, "get_reschedule_time_slots")
            response.raise_for_status()
            data = _unwrap(response.json())
        except httpx.HTTPError as exc:
            logger.exception("Failed to get reschedule time slots")
            return f"Sorry, I couldn't fetch time slots for {_format_date_display(date)}. Error: {exc}"
    else:
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
            return f"Sorry, I couldn't fetch time slots for {_format_date_display(date)}. Error: {exc}"

    slots = data.get("slots", data.get("timeSlots", []))
    if not slots:
        return f"No time slots available for {_format_date_display(date)}. Please try another date."

    # Cache request_id internally for confirm_appointment — do NOT expose
    # to the LLM (it confuses request_id with project_id).
    api_rid = data.get("request_id", request_id)
    if api_rid:
        _request_id_by_project[project_id] = api_rid
    pnum = _get_project_number(project_id)
    date_display = _format_date_display(date)
    result = {
        "project_number": pnum,
        "date": date_display,
        "time_slots": slots,
        "message": f"Found {len(slots)} available time slot(s) for {date_display}.",
    }
    return json.dumps(result, indent=2)


async def confirm_appointment(project_id: str, date: str, time: str, **kwargs) -> str:
    """Schedule an appointment. Only call AFTER the customer has confirmed."""
    _confirm_called_in_request.set(True)
    try:
        return await _confirm_appointment_impl(project_id, date, time, **kwargs)
    except (httpx.HTTPError, json.JSONDecodeError):
        raise  # Let known errors propagate for specific handling
    except Exception:
        logger.exception("Unexpected error in confirm_appointment (project=%s)", project_id)
        return "Sorry, something went wrong while scheduling. Please try again."


async def _confirm_appointment_impl(project_id: str, date: str, time: str, **kwargs) -> str:
    """Internal implementation of confirm_appointment."""
    project_id = _resolve_project_id(project_id)
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
        return f"Sorry, I couldn't schedule the appointment. Error: {exc}"

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
    msg = f"Appointment confirmed! Your appointment is scheduled for {date_display} at {time_display}."
    if confirmation_number:
        msg += f" Confirmation number: {confirmation_number}."
    msg += " You will receive a confirmation to your registered email and phone number."
    return msg


async def reschedule_appointment(project_id: str) -> str:
    """Start reschedule flow — cancels existing appointment and fetches new dates.

    Uses the dedicated ``get-reschedule-slotsChatBot`` API endpoint which
    ignores current appointment status and returns alternative availability.
    """
    _reschedule_called_in_request.set(True)
    try:
        return await _reschedule_appointment_impl(project_id)
    except Exception:
        logger.exception("Unexpected error in reschedule_appointment (project=%s)", project_id)
        return "Sorry, something went wrong during rescheduling. Please try again."


async def _reschedule_appointment_impl(project_id: str) -> str:
    """Internal implementation of reschedule_appointment."""
    project_id = _resolve_project_id(project_id)
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

    # PF's reschedule endpoint handles cancel + new slots atomically.
    # Do NOT call cancel_appointment separately — if the customer declines
    # the new slots, the project stays in "reschedule pending" state instead
    # of being hard-cancelled.
    reschedule_dates = await _get_reschedule_slots(project_id)
    if reschedule_dates:
        _invalidate_projects()
        _reschedule_pending.add(project_id)
        return reschedule_dates

    # _get_reschedule_slots may have successfully cancelled but returned no
    # dates (API returns only a message).  If it set _reschedule_pending,
    # guide the LLM to fetch dates via the normal flow.
    if project_id in _reschedule_pending:
        return (
            "The existing appointment has been cancelled. "
            "Now call get_available_dates to fetch available dates for rescheduling."
        )

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

    # Include time slots from the API response
    raw_slots = data.get("slots", [])
    formatted_slots = []
    for s in raw_slots:
        try:
            h, m, *_ = s.split(":")
            hour = int(h)
            minute = m
            period = "AM" if hour < 12 else "PM"
            display_hour = hour if hour <= 12 else hour - 12
            if display_hour == 0:
                display_hour = 12
            formatted_slots.append(f"{display_hour}:{minute} {period}")
        except (ValueError, IndexError):
            formatted_slots.append(s)

    slots_msg = ""
    if formatted_slots:
        slots_msg = f" Available time slots on each date: {', '.join(formatted_slots)}."

    pnum = _get_project_number(project_id)
    result: dict[str, Any] = {
        "project_number": pnum,
        "is_reschedule": True,
        "available_dates": dates,
        "message": (
            f"The existing appointment has been cancelled. "
            f"Found {len(dates)} new date(s) for rescheduling.{slots_msg}"
        ),
    }

    if formatted_slots:
        result["available_time_slots"] = formatted_slots

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
    try:
        return await _cancel_appointment_impl(project_id, reason)
    except Exception:
        logger.exception("Unexpected error in cancel_appointment (project=%s)", project_id)
        return "Sorry, something went wrong while cancelling. Please try again."


async def _cancel_appointment_impl(project_id: str, reason: str = "") -> str:
    """Internal implementation of cancel_appointment."""
    project_id = _resolve_project_id(project_id)
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
        return f"Sorry, I couldn't cancel the appointment. Error: {exc}"

    # Invalidate project cache — status changed
    _invalidate_projects()
    _reschedule_pending.discard(project_id)

    # Auto-save cancellation reason note if provided
    if reason.strip():
        note_text = f"CANCELLATION REASON: {reason.strip()}. Cancelled via AI Scheduling Assistant."
        try:
            note_result = await add_note(project_id, note_text)
            logger.info("Cancel note saved for project %s: %s", project_id, note_result)
            if "sorry" in note_result.lower() or "couldn't" in note_result.lower():
                return (
                    "Appointment has been cancelled successfully. "
                    "However, I was unable to save the cancellation reason note. "
                    "Please try adding the note manually."
                )
        except Exception:
            logger.exception("Failed to save cancellation note for project %s", project_id)
            return (
                "Appointment has been cancelled successfully. "
                "However, I was unable to save the cancellation reason note. "
                "Please try adding the note manually."
            )

    return "Appointment has been cancelled successfully."


async def add_note(project_id: str, note_text: str) -> str:
    """Add a note to a project."""
    project_id = _resolve_project_id(project_id)
    _track_project_action(project_id, "add_note")
    client_id = AuthContext.get_client_id()
    caller_type = AuthContext.get_caller_type()
    base = get_pf_api_base()
    headers = build_headers()

    # Cancel/reschedule reason notes always use /project-notes/add-note
    # (both store and customer). All other store notes also use it.
    # Customer general notes use /communication/.../note.
    # Address corrections use /project-notes/update-address-note.
    is_cancel_note = any(
        prefix in note_text.upper()
        for prefix in ("CANCELLATION REASON:", "CANCELLATION:", "RESCHEDULE REASON:")
    )
    is_address_note = "ADDRESS CORRECTION:" in note_text.upper()

    try:
        pid = int(project_id)
    except (ValueError, TypeError):
        pid = project_id

    if is_address_note and caller_type != "store":
        # Customer address corrections (VAPI inbound/outbound):
        # POST /project-notes/update-address-note
        url = f"{base}/project-notes/update-address-note"
        payload = {
            "client_id": client_id,
            "project_id": pid,
            "note_text": note_text,
        }
    elif caller_type == "store" or is_cancel_note:
        # Store callers OR cancel/reschedule reasons: POST /project-notes/add-note
        url = f"{base}/project-notes/add-note"
        payload = {
            "client_id": client_id,
            "project_id": pid,
            "note_text": note_text,
        }
    else:
        # Customer general notes: POST /communication/client/{cid}/project/{pid}/note
        url = f"{base}/communication/client/{client_id}/project/{project_id}/note"
        payload = {"note_text": note_text}

    log_curl("POST", url, headers, payload)

    try:
        async with httpx.AsyncClient(timeout=_SCHEDULER_TIMEOUT) as client:
            response = await client.post(url, headers=headers, json=payload)
        log_response(response, "add_note")
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.exception("Failed to add note")
        return f"Sorry, I couldn't add the note. Error: {exc}"

    pnum = _get_project_number(project_id)
    return f"Note added successfully to project {pnum}."


async def list_notes(project_id: str) -> str:
    """List notes for a project."""
    project_id = _resolve_project_id(project_id)
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
        return f"Sorry, I couldn't fetch notes. Error: {exc}"

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
) -> None:
    """Post call summary as a note to each project discussed during the session.

    Called from the Vapi end-of-call handler. Uses explicit auth params since
    AuthContext is not available outside the request lifecycle.
    """
    projects_discussed = get_session_projects(session_id)
    if not projects_discussed:
        logger.info("No projects discussed in session %s — skipping call notes", session_id)
        clear_session_projects(session_id)
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

    truncated_summary = (summary[:200] + "...") if len(summary) > 200 else summary

    async with httpx.AsyncClient(timeout=15.0) as client:
        for project_id, actions in projects_discussed.items():
            action_list = ", ".join(actions) if actions else "general inquiry"
            note_text = (
                f"Customer called on {call_time} via AI Scheduling Assistant (J). "
                f"Duration: {duration_str}. Actions: {action_list}."
            )
            if truncated_summary:
                note_text += f" Summary: {truncated_summary}"

            url = f"{base_url}/communication/client/{client_id}/project/{project_id}/note"
            payload = {"note_text": note_text}
            try:
                resp = await client.post(url, headers=headers, json=payload)
                logger.info(
                    "Call note posted: project=%s status=%d session=%s",
                    project_id, resp.status_code, session_id,
                )
            except Exception:
                logger.exception(
                    "Failed to post call note for project %s (session=%s)",
                    project_id, session_id,
                )

    clear_session_projects(session_id)


async def post_store_call_notes(
    *,
    session_id: str,
    bearer_token: str,
    client_id: str,
    summary: str,
    duration_seconds: float = 0,
) -> None:
    """Post call summary as a note for store calls via /project-notes/add-note.

    Store callers don't have customer-level auth, so we use the separate
    add-note endpoint that only requires client_id + project_id.
    Called from the Vapi end-of-call handler for store sessions.
    """
    projects_discussed = get_session_projects(session_id)
    if not projects_discussed:
        logger.info("No projects discussed in store session %s — skipping call notes", session_id)
        clear_session_projects(session_id)
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

    truncated_summary = (summary[:200] + "...") if len(summary) > 200 else summary

    async with httpx.AsyncClient(timeout=15.0) as client:
        for project_id, actions in projects_discussed.items():
            action_list = ", ".join(actions) if actions else "general inquiry"
            note_text = (
                f"Store called on {call_time} via AI Scheduling Assistant (J). "
                f"Duration: {duration_str}. Actions: {action_list}."
            )
            if truncated_summary:
                note_text += f" Summary: {truncated_summary}"

            url = f"{base_url}/project-notes/add-note"
            payload = {
                "client_id": client_id,
                "project_id": int(project_id),
                "note_text": note_text,
            }
            try:
                resp = await client.post(url, headers=headers, json=payload)
                logger.info(
                    "Store call note posted: project=%s status=%d session=%s",
                    project_id, resp.status_code, session_id,
                )
            except Exception:
                logger.exception(
                    "Failed to post store call note for project %s (session=%s)",
                    project_id, session_id,
                )

    clear_session_projects(session_id)


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
        return f"Sorry, I couldn't fetch business hours. Error: {exc}"

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
    project_id = _resolve_project_id(project_id)
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
    """Update the installation address for a project.

    Currently returns a simulated success response.

    TODO: uncomment API call when /authentication/update-installation-address is ready.
    """
    _confirm_called_in_request.set(True)
    project_id = _resolve_project_id(project_id)
    _track_project_action(project_id, "update_installation_address")

    # --- API call commented out for now ---
    # # Get address_id from cache
    # cached = _get_cached_project(project_id)
    # address_id = ""
    # if cached and cached.get("address"):
    #     address_id = str(cached["address"].get("address_id", ""))
    #
    # # If no address_id cached, fetch it via the get API
    # if not address_id:
    #     logger.info("No cached address_id for project %s — fetching", project_id)
    #     get_result = await get_installation_address(project_id)
    #     try:
    #         get_data = json.loads(get_result)
    #         address_id = str(get_data.get("address", {}).get("address_id", ""))
    #     except (json.JSONDecodeError, TypeError):
    #         pass
    #
    # if not address_id:
    #     return (
    #         f"Cannot update address for project {project_id}: "
    #         "no address_id found. The project may not have an address on file."
    #     )
    #
    # client_id = AuthContext.get_client_id()
    # base_url = get_pf_api_base()
    # url = f"{base_url}/authentication/update-installation-address"
    # headers = build_headers()
    # body: dict[str, Any] = {
    #     "address_id": int(address_id),
    #     "client_id": client_id,
    #     "address1": address1,
    #     "city": city,
    # }
    # if state:
    #     body["state"] = state
    # if zipcode:
    #     body["zipcode"] = zipcode
    #
    # try:
    #     async with httpx.AsyncClient(timeout=_SCHEDULER_TIMEOUT) as client:
    #         log_curl("PUT", url, headers, body)
    #         resp = await client.put(url, headers=headers, json=body)
    #         log_response(resp, "update-installation-address")
    #         resp.raise_for_status()
    #
    #         _invalidate_projects()
    #         updated_address = {
    #             "address1": address1,
    #             "city": city,
    #             "state": state,
    #             "zipcode": zipcode,
    #         }
    #         return json.dumps({
    #             "project_id": project_id,
    #             "updated_address": {k: v for k, v in updated_address.items() if v},
    #             "message": f"Installation address updated for project {project_id}.",
    #         })
    # except httpx.HTTPStatusError as exc:
    #     logger.error(
    #         "update-installation-address failed: %d %s",
    #         exc.response.status_code,
    #         exc.response.text[:500],
    #     )
    #     return (
    #         f"Failed to update the address for project {project_id}. "
    #         "Please try again or contact support."
    #     )
    # except Exception:
    #     logger.exception("update-installation-address error for project %s", project_id)
    #     return (
    #         f"Failed to update the address for project {project_id}. "
    #         "Please try again or contact support."
    #     )

    # Feature not yet available — offer transfer or number on phone, contact office on chat
    logger.info("update_installation_address (not available) for project %s", project_id)

    support_number = AuthContext.get_support_number()

    result: dict = {
        "project_number": _get_project_number(project_id),
        "feature_unavailable": True,
        "message": (
            "Updating the installation address through the bot is not available yet. "
            "The office can help with address updates."
        ),
    }
    if support_number:
        result["support_number"] = support_number

    return json.dumps(result)
