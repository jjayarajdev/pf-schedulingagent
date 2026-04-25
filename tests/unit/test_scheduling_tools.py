"""Tests for scheduling tool handlers."""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.scheduling import (
    _build_intelligent_fallback,
    _extract_project_minimal,
    _get_reschedule_slots,
    _match_scheduled_date,
    _match_scheduled_month,
    add_note,
    cancel_appointment,
    confirm_appointment,
    get_available_dates,
    get_business_hours,
    get_installation_address,
    get_project_details,
    get_session_notes,
    get_time_slots,
    list_notes,
    list_projects,
    reschedule_appointment,
    update_installation_address,
)


@pytest.fixture(autouse=True)
def _set_auth(mock_auth):
    """All scheduling tool tests need auth context."""


class TestListProjects:
    async def test_returns_projects(self, mock_httpx_client, mock_httpx_response):
        response = mock_httpx_response(
            200,
            {
                "data": [
                    {
                        "project_project_id": "123",
                        "project_project_number": "P-001",
                        "status_info_status": "New",
                        "project_category_category": "Flooring",
                        "project_type_project_type": "Install",
                        "store_info_store_name": "Store A",
                        "store_info_store_number": "100",
                    }
                ]
            },
        )
        with mock_httpx_client(response=response):
            result = await list_projects()
        data = json.loads(result)
        assert data["projects"][0]["id"] == "123"
        assert data["projects"][0]["status"] == "New"

    async def test_filters_closed_projects(self, mock_httpx_client, mock_httpx_response):
        response = mock_httpx_response(
            200,
            {
                "data": [
                    {
                        "project_project_id": "1",
                        "status_info_status": "Completed",
                        "store_info_store_name": "",
                        "store_info_store_number": "",
                    },
                    {
                        "project_project_id": "2",
                        "status_info_status": "New",
                        "store_info_store_name": "",
                        "store_info_store_number": "",
                    },
                ]
            },
        )
        with mock_httpx_client(response=response):
            result = await list_projects()
        data = json.loads(result)
        assert len(data["projects"]) == 1
        assert data["projects"][0]["id"] == "2"

    async def test_no_customer_id(self):
        from auth.context import AuthContext

        AuthContext.set(customer_id="")
        result = await list_projects()
        assert "Error" in result or "customer" in result.lower()

    async def test_status_filter(self, mock_httpx_client, mock_httpx_response):
        response = mock_httpx_response(
            200,
            {
                "data": [
                    {
                        "project_project_id": "1",
                        "status_info_status": "New",
                        "store_info_store_name": "",
                        "store_info_store_number": "",
                    },
                    {
                        "project_project_id": "2",
                        "status_info_status": "Scheduled",
                        "store_info_store_name": "",
                        "store_info_store_number": "",
                    },
                ]
            },
        )
        with mock_httpx_client(response=response):
            result = await list_projects(status="scheduled")
        data = json.loads(result)
        assert len(data["projects"]) == 1

    async def test_schedulable_filter(self, mock_httpx_client, mock_httpx_response):
        response = mock_httpx_response(
            200,
            {
                "data": [
                    {"project_project_id": "1", "status_info_status": "New",
                     "store_info_store_name": "", "store_info_store_number": ""},
                    {"project_project_id": "2", "status_info_status": "Scheduled",
                     "store_info_store_name": "", "store_info_store_number": ""},
                    {"project_project_id": "3", "status_info_status": "Ready to Schedule",
                     "store_info_store_name": "", "store_info_store_number": ""},
                ]
            },
        )
        with mock_httpx_client(response=response):
            result = await list_projects(status="schedulable")
        data = json.loads(result)
        assert len(data["projects"]) == 2
        ids = {p["id"] for p in data["projects"]}
        assert ids == {"1", "3"}

    async def test_project_type_filter(self, mock_httpx_client, mock_httpx_response):
        response = mock_httpx_response(
            200,
            {
                "data": [
                    {"project_project_id": "1", "status_info_status": "New",
                     "project_type_project_type": "Windows Installation",
                     "store_info_store_name": "", "store_info_store_number": ""},
                    {"project_project_id": "2", "status_info_status": "New",
                     "project_type_project_type": "Fence Installation",
                     "store_info_store_name": "", "store_info_store_number": ""},
                ]
            },
        )
        with mock_httpx_client(response=response):
            result = await list_projects(project_type="Windows")
        data = json.loads(result)
        assert len(data["projects"]) == 1
        assert data["projects"][0]["id"] == "1"

    async def test_intelligent_fallback_all_scheduled(self, mock_httpx_client, mock_httpx_response):
        """When no schedulable projects, show intelligent fallback."""
        response = mock_httpx_response(
            200,
            {
                "data": [
                    {"project_project_id": "1", "status_info_status": "Scheduled",
                     "project_category_category": "Roofing",
                     "convertedProjectStartScheduledDate": "2026-04-01",
                     "store_info_store_name": "", "store_info_store_number": ""},
                    {"project_project_id": "2", "status_info_status": "Scheduled",
                     "project_category_category": "Fencing",
                     "store_info_store_name": "", "store_info_store_number": ""},
                ]
            },
        )
        with mock_httpx_client(response=response):
            result = await list_projects(status="schedulable")
        data = json.loads(result)
        assert data["already_scheduled"] is True
        assert "reschedule" in data["message"].lower()

    async def test_intelligent_fallback_on_hold(self, mock_httpx_client, mock_httpx_response):
        response = mock_httpx_response(
            200,
            {
                "data": [
                    {"project_project_id": "1", "status_info_status": "Waiting for Product",
                     "project_category_category": "Windows",
                     "store_info_store_name": "", "store_info_store_number": ""},
                ]
            },
        )
        with mock_httpx_client(response=response):
            result = await list_projects(status="schedulable")
        assert "Waiting for Product" in result
        assert "cannot be scheduled" in result

    async def test_scheduled_month_filter(self, mock_httpx_client, mock_httpx_response):
        response = mock_httpx_response(
            200,
            {
                "data": [
                    {"project_project_id": "1", "status_info_status": "Scheduled",
                     "convertedProjectStartScheduledDate": "2026-03-15",
                     "store_info_store_name": "", "store_info_store_number": ""},
                    {"project_project_id": "2", "status_info_status": "Scheduled",
                     "convertedProjectStartScheduledDate": "2026-04-10",
                     "store_info_store_name": "", "store_info_store_number": ""},
                ]
            },
        )
        with mock_httpx_client(response=response):
            result = await list_projects(scheduled_month="March")
        data = json.loads(result)
        assert len(data["projects"]) == 1
        assert data["projects"][0]["id"] == "1"


    async def test_filters_all_terminal_statuses(self, mock_httpx_client, mock_httpx_response):
        """Completed variants like 'Project Completed', 'Work Order Completed' must be excluded."""
        response = mock_httpx_response(
            200,
            {
                "data": [
                    {"project_project_id": "1", "status_info_status": "Project Completed",
                     "store_info_store_name": "", "store_info_store_number": ""},
                    {"project_project_id": "2", "status_info_status": "Work Order Completed",
                     "store_info_store_name": "", "store_info_store_number": ""},
                    {"project_project_id": "3", "status_info_status": "Cancelled/Surge",
                     "store_info_store_name": "", "store_info_store_number": ""},
                    {"project_project_id": "4", "status_info_status": "Completed-Archived",
                     "store_info_store_name": "", "store_info_store_number": ""},
                    {"project_project_id": "5", "status_info_status": "Scheduled",
                     "store_info_store_name": "", "store_info_store_number": ""},
                    {"project_project_id": "6", "status_info_status": "Ready To Schedule",
                     "store_info_store_name": "", "store_info_store_number": ""},
                ]
            },
        )
        with mock_httpx_client(response=response):
            result = await list_projects()
        data = json.loads(result)
        assert len(data["projects"]) == 2
        ids = {p["id"] for p in data["projects"]}
        assert ids == {"5", "6"}


class TestGetProjectDetails:
    async def test_returns_details(self, mock_httpx_client, mock_httpx_response):
        """get_project_details calls the dashboard endpoint and filters by project_id."""
        response = mock_httpx_response(
            200,
            {
                "data": [
                    {
                        "project_project_id": "123",
                        "project_project_number": "PRJ-123",
                        "status_info_status": "New",
                        "project_category_category": "Install",
                        "store_info_store_name": "Home Depot #1234",
                        "store_info_store_number": "1234",
                    },
                    {
                        "project_project_id": "456",
                        "status_info_status": "Scheduled",
                        "store_info_store_name": "",
                        "store_info_store_number": "",
                    },
                ]
            },
        )
        with mock_httpx_client(response=response):
            result = await get_project_details("123")
        data = json.loads(result)
        assert data["project"]["id"] == "123"
        assert data["project"]["status"] == "New"
        assert "message" in data

    async def test_project_not_found(self, mock_httpx_client, mock_httpx_response):
        response = mock_httpx_response(200, {"data": [{"project_project_id": "999", "status_info_status": "New", "store_info_store_name": "", "store_info_store_number": ""}]})
        with mock_httpx_client(response=response):
            result = await get_project_details("123")
        assert "not found" in result.lower()


class TestGetAvailableDates:
    async def test_returns_dates(self, mock_httpx_client, mock_httpx_response):
        response = mock_httpx_response(
            200,
            {
                "dates": ["2026-03-15", "2026-03-16"],
                "request_id": 90001234,
            },
        )
        with mock_httpx_client(response=response):
            result = await get_available_dates("123")
        data = json.loads(result)
        assert len(data["available_dates"]) == 2

    async def test_no_dates_expands_range(self, mock_httpx_client, mock_httpx_response):
        empty_resp = mock_httpx_response(200, {"dates": [], "request_id": 0})
        expanded_resp = mock_httpx_response(
            200, {"dates": ["2026-03-20"], "request_id": 90001235}
        )
        with mock_httpx_client(responses=[empty_resp, expanded_resp]):
            result = await get_available_dates("123", start_date="2026-03-10")
        data = json.loads(result)
        assert len(data["available_dates"]) == 1

    async def test_already_scheduled_returns_structured_response(self, mock_httpx_client, mock_httpx_response):
        """400 with 'already scheduled' message returns structured reschedule offer."""
        response = mock_httpx_response(
            400,
            None,
            text="This project is already scheduled and contains a technician assignment",
        )
        # Override raise_for_status so it won't be called (we handle 400 before it)
        response.raise_for_status = MagicMock()
        with mock_httpx_client(response=response):
            result = await get_available_dates("123")
        data = json.loads(result)
        assert data["already_scheduled"] is True
        assert data["available_dates"] == []
        assert "reschedule" in data["message"].lower()

    async def test_already_requested_returns_structured_response(self, mock_httpx_client, mock_httpx_response):
        response = mock_httpx_response(
            400,
            None,
            text="This date has already requested for this project",
        )
        response.raise_for_status = MagicMock()
        with mock_httpx_client(response=response):
            result = await get_available_dates("456")
        data = json.loads(result)
        assert data["already_scheduled"] is True


    async def test_dates_response_never_includes_time_slots(self, mock_httpx_client, mock_httpx_response):
        """Dates response must NEVER include time slots — forces get_time_slots call."""
        response = mock_httpx_response(
            200,
            {"dates": ["2026-04-15", "2026-04-16"], "slots": ["08:00:00", "13:00:00"], "request_id": 90001393},
        )
        with mock_httpx_client(response=response):
            result = await get_available_dates("123")
        data = json.loads(result)
        assert "available_time_slots" not in data
        # LLM instructions are in _llm_instruction, not in the customer-facing message
        assert "MUST call get_time_slots" in data["_llm_instruction"]
        assert "Do NOT guess" in data["_llm_instruction"]
        # Customer-facing message should be clean
        assert "MUST call" not in data["message"]

    async def test_empty_slots_also_instructs_get_time_slots(self, mock_httpx_client, mock_httpx_response):
        """Even with empty slots, response tells LLM to call get_time_slots."""
        response = mock_httpx_response(
            200,
            {"dates": ["2026-04-15", "2026-04-16"], "slots": [], "request_id": 90001393},
        )
        with mock_httpx_client(response=response):
            result = await get_available_dates("123")
        data = json.loads(result)
        assert "available_time_slots" not in data
        assert "MUST call get_time_slots" in data["_llm_instruction"]

    async def test_past_date_clamped_to_tomorrow(self, mock_httpx_client, mock_httpx_response):
        """Past start_date is silently clamped to tomorrow — API still called."""
        response = mock_httpx_response(
            200,
            {"dates": ["2026-05-01"], "request_id": 90001236},
        )
        past_date = "2025-01-15"
        with mock_httpx_client(response=response):
            result = await get_available_dates("123", start_date=past_date)
        data = json.loads(result)
        # Should still return dates (clamped, not rejected)
        assert len(data["available_dates"]) == 1


class TestGetTimeSlots:
    async def test_returns_slots(self, mock_httpx_client, mock_httpx_response):
        response = mock_httpx_response(
            200,
            {"slots": [{"time": "9:00 AM", "id": "slot-1"}, {"time": "10:00 AM", "id": "slot-2"}]},
        )
        future_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        with mock_httpx_client(response=response):
            result = await get_time_slots("123", future_date)
        data = json.loads(result)
        assert len(data["time_slots"]) == 2

    async def test_no_slots(self, mock_httpx_client, mock_httpx_response):
        response = mock_httpx_response(200, {"slots": []})
        future_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        with mock_httpx_client(response=response):
            result = await get_time_slots("123", future_date)
        assert "No time slots" in result

    async def test_past_date_rejected(self, mock_httpx_client, mock_httpx_response):
        """Past date in get_time_slots returns an error, not API call."""
        # Use a date in the current year that's clearly in the past
        result = await get_time_slots("123", "2026-01-01")
        data = json.loads(result)
        assert data["error"] == "past_date"
        assert "already passed" in data["message"]
        assert "01/01/2026" in data["requested_date"]


class TestConfirmAppointment:
    async def test_confirm_schedules(self, mock_httpx_client, mock_httpx_response):
        schedule_resp = mock_httpx_response(200, {"confirmationNumber": "CONF-789"})
        future_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        with mock_httpx_client(responses=[schedule_resp]):
            result = await confirm_appointment("123", future_date, "9:00 AM")
        assert "confirmed" in result.lower()
        assert "CONF-789" in result


class TestRescheduleAppointment:
    async def test_reschedule_with_rescheduler_api(self, mock_httpx_client, mock_httpx_response):
        """Reschedule uses PF's atomic cancel+reschedule endpoint (no separate cancel call)."""
        from tools.scheduling import _projects_cache
        from datetime import datetime, timezone

        _projects_cache["test-customer-456"] = {
            "projects": [
                {"id": "100", "status": "Scheduled", "scheduledDate": "2026-03-10",
                 "category": "Fencing", "address": {}},
            ],
            "loaded_at": datetime.now(timezone.utc),
        }

        reschedule_resp = mock_httpx_response(200, {
            "data": {
                "dates": ["2026-03-20", "2026-03-21", "2026-03-22"],
                "request_id": 5555,
            }
        })

        with mock_httpx_client(responses=[reschedule_resp]):
            result = await reschedule_appointment("100")

        data = json.loads(result)
        assert data["is_reschedule"] is True
        assert len(data["available_dates"]) == 3

    async def test_reschedule_fallback_when_api_fails(self, mock_httpx_client, mock_httpx_response):
        """Falls back to helpful message when rescheduler API fails."""
        from tools.scheduling import _projects_cache, _reschedule_pending
        from datetime import datetime, timezone

        _reschedule_pending.discard("100")
        _projects_cache["test-customer-456"] = {
            "projects": [
                {"id": "100", "status": "Scheduled", "scheduledDate": "2026-03-10",
                 "category": "Fencing", "address": {}},
            ],
            "loaded_at": datetime.now(timezone.utc),
        }

        reschedule_resp = mock_httpx_response(500, None, text="Internal Server Error")

        with mock_httpx_client(responses=[reschedule_resp]):
            result = await reschedule_appointment("100")

        assert "try again" in result.lower()
        assert "get_available_dates" in result

    async def test_reschedule_then_confirm_skips_status_check(
        self, mock_httpx_client, mock_httpx_response
    ):
        """After reschedule returns dates, confirm_appointment must skip the
        'already scheduled' status check — PF's atomic endpoint already cancelled."""
        from tools.scheduling import _projects_cache, _reschedule_pending
        from datetime import datetime, timezone

        # Pre-populate cache as "Scheduled" (simulates cache reload between turns)
        _projects_cache["test-customer-456"] = {
            "projects": [
                {"id": "100", "projectNumber": "74356", "status": "Scheduled",
                 "scheduledDate": "2026-04-17", "category": "Balcony grill",
                 "address": {}},
            ],
            "loaded_at": datetime.now(timezone.utc),
        }

        # Step 1: reschedule_appointment → marks project as reschedule-pending
        reschedule_resp = mock_httpx_response(200, {
            "data": {
                "dates": ["2026-04-18", "2026-04-19"],
                "request_id": 9999,
            }
        })
        with mock_httpx_client(responses=[reschedule_resp]):
            result = await reschedule_appointment("100")

        data = json.loads(result)
        assert data["is_reschedule"] is True
        assert "100" in _reschedule_pending

        # Simulate cache reload (LLM called list_projects between turns)
        _projects_cache["test-customer-456"] = {
            "projects": [
                {"id": "100", "projectNumber": "74356", "status": "Scheduled",
                 "scheduledDate": "2026-04-17", "category": "Balcony grill",
                 "address": {}},
            ],
            "loaded_at": datetime.now(timezone.utc),
        }

        # Step 2: confirm_appointment — must NOT block with "already scheduled"
        confirm_resp = mock_httpx_response(200, {
            "data": True, "confirmationNumber": "CONF-001"
        })
        with mock_httpx_client(responses=[confirm_resp]):
            result = await confirm_appointment("100", "2026-04-18", "08:00:00")

        assert "confirmed" in result.lower()
        assert "100" not in _reschedule_pending  # flag cleared


class TestCancelAppointment:
    async def test_cancel_without_reason(self, mock_httpx_client, mock_httpx_response):
        """Cancel without reason makes only the cancel API call."""
        cancel_resp = mock_httpx_response(200, {"data": {"message": "Cancel successfully"}})
        with mock_httpx_client(responses=[cancel_resp]):
            result = await cancel_appointment("123")
        assert "cancelled" in result.lower()

    async def test_cancel_with_reason_saves_note(self, mock_httpx_client, mock_httpx_response):
        """Cancel with reason auto-saves cancellation note via add_note."""
        cancel_resp = mock_httpx_response(200, {"data": {"message": "Cancel successfully"}})
        note_resp = mock_httpx_response(200, {"status": "ok"})
        with mock_httpx_client(responses=[cancel_resp, note_resp]):
            result = await cancel_appointment("123", reason="personal appointment conflict")
        assert "cancelled" in result.lower()

    async def test_cancel_with_reason_note_failure_still_returns_success(
        self, mock_httpx_response
    ):
        """If cancel succeeds but note fails, report partial success."""
        cancel_resp = mock_httpx_response(200, {"data": {"message": "Cancel successfully"}})
        note_resp = mock_httpx_response(500, None, text="Internal Server Error")

        # cancel uses .get(), add_note uses .post() — need separate side_effects
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=cancel_resp)
        mock_client.post = AsyncMock(return_value=note_resp)

        mock_context = AsyncMock()
        mock_context.__aenter__ = AsyncMock(return_value=mock_client)
        mock_context.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_context):
            result = await cancel_appointment("123", reason="moving to new address")
        assert "cancelled" in result.lower()
        assert "unable to save" in result.lower()


class TestAddNote:
    async def test_adds_note(self, mock_httpx_client, mock_httpx_response):
        response = mock_httpx_response(200, {"status": "ok"})
        with mock_httpx_client(response=response):
            result = await add_note("123", "Test note")
        assert "successfully" in result.lower()

    async def test_customer_note_posts_immediately_for_chat(self):
        """Chat channel: customer notes post immediately to /communication/.../note."""
        from observability.logging import RequestContext

        RequestContext.set(session_id="test-session", channel="chat")
        try:
            mock_resp = MagicMock(status_code=200)
            mock_resp.json.return_value = {"status": "ok"}
            mock_resp.raise_for_status = MagicMock()
            mock_resp.request = MagicMock(method="POST")
            mock_resp.url = "https://test.com"

            with patch("tools.scheduling.httpx.AsyncClient") as mock_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = mock_resp
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_cls.return_value = mock_client

                result = await add_note("123", "General customer note")

            assert "Note added successfully" in result
            url = mock_client.post.call_args[0][0]
            assert "/communication/client/" in url
            assert "/project/123/note" in url
        finally:
            RequestContext.clear()

    async def test_customer_note_caches_for_vapi_channel(self):
        """Vapi channel: customer notes are cached for deferred end-of-call posting."""
        from observability.logging import RequestContext

        RequestContext.set(session_id="test-session", channel="vapi")
        try:
            result = await add_note("123", "General customer note")
            assert "Note added successfully" in result

            notes = get_session_notes("test-session")
            assert "123" in notes
            assert "General customer note" in notes["123"]
        finally:
            RequestContext.clear()

    async def test_customer_cancel_note_uses_add_note_endpoint(self):
        """Customer cancel/reschedule reason notes route to /project-notes/add-note."""
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"status": "ok"}
        mock_resp.raise_for_status = MagicMock()
        mock_resp.request = MagicMock(method="POST")
        mock_resp.url = "https://test.com"

        with patch("tools.scheduling.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            await add_note("123", "CANCELLATION REASON: Schedule conflict. Cancelled via AI.")

        url = mock_client.post.call_args[0][0]
        payload = mock_client.post.call_args[1].get("json", {})
        assert "/project-notes/add-note" in url
        assert payload["client_id"] == "test-client-123"
        assert payload["project_id"] == 123

    async def test_store_note_uses_add_note_endpoint(self):
        """Store callers always route to /project-notes/add-note."""
        from auth.context import AuthContext

        AuthContext.set(
            auth_token="tok", client_id="CL1", customer_id="C1",
            caller_type="store",
        )
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"status": "ok"}
        mock_resp.raise_for_status = MagicMock()
        mock_resp.request = MagicMock(method="POST")
        mock_resp.url = "https://test.com"

        with patch("tools.scheduling.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            await add_note("456", "Store note")

        url = mock_client.post.call_args[0][0]
        assert "/project-notes/add-note" in url


    async def test_address_correction_uses_update_address_endpoint(self):
        """Customer address correction notes route to /project-notes/update-address-note."""
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"status": "ok"}
        mock_resp.raise_for_status = MagicMock()
        mock_resp.request = MagicMock(method="POST")
        mock_resp.url = "https://test.com"

        with patch("tools.scheduling.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            await add_note("123", "CUSTOMER REQUESTED INSTALLATION ADDRESS UPDATE. New address is 910 North Harbor Drive, San Diego, CA 92101")

        url = mock_client.post.call_args[0][0]
        payload = mock_client.post.call_args[1].get("json", {})
        assert "/project-notes/update-address-note" in url
        assert payload["client_id"] == "test-client-123"
        assert payload["project_id"] == 123
        assert "CUSTOMER REQUESTED INSTALLATION ADDRESS UPDATE" in payload["note_text"]

    async def test_store_address_correction_uses_add_note_endpoint(self):
        """Store caller address corrections route to /project-notes/add-note, not update-address."""
        from auth.context import AuthContext

        AuthContext.set(
            auth_token="tok", client_id="CL1", customer_id="C1",
            caller_type="store",
        )
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"status": "ok"}
        mock_resp.raise_for_status = MagicMock()
        mock_resp.request = MagicMock(method="POST")
        mock_resp.url = "https://test.com"

        with patch("tools.scheduling.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            await add_note("456", "ADDRESS CORRECTION: 100 New Street, Dallas, TX 75201")

        url = mock_client.post.call_args[0][0]
        assert "/project-notes/add-note" in url
        assert "/update-address-note" not in url


class TestListNotes:
    async def test_returns_notes(self, mock_httpx_client, mock_httpx_response):
        response = mock_httpx_response(
            200, {"notes": [{"text": "Note 1"}, {"text": "Note 2"}]}
        )
        with mock_httpx_client(response=response):
            result = await list_notes("123")
        data = json.loads(result)
        assert data["count"] == 2


class TestGetBusinessHours:
    async def test_returns_hours(self, mock_httpx_client, mock_httpx_response):
        response = mock_httpx_response(
            200, {"hours": {"monday": "8:00-17:00", "tuesday": "8:00-17:00"}}
        )
        with mock_httpx_client(response=response):
            result = await get_business_hours()
        data = json.loads(result)
        assert "hours" in data


class TestIntelligentFallback:
    def test_all_scheduled(self):
        projects = [
            {"id": "1", "status": "Scheduled", "category": "Roofing", "scheduledDate": "2026-04-01"},
            {"id": "2", "status": "Scheduled", "category": "Fencing"},
        ]
        result = _build_intelligent_fallback(projects)
        data = json.loads(result)
        assert data["already_scheduled"] is True
        assert "reschedule" in data["message"].lower()

    def test_all_cancelled(self):
        projects = [{"id": "1", "status": "Cancelled", "category": "Windows"}]
        result = _build_intelligent_fallback(projects)
        assert "cancelled" in result.lower()
        assert "cannot be scheduled" in result.lower()

    def test_all_completed(self):
        projects = [{"id": "1", "status": "Completed", "category": "Decking"}]
        result = _build_intelligent_fallback(projects)
        assert "completed" in result.lower()

    def test_on_hold(self):
        projects = [{"id": "1", "status": "Waiting for Product", "category": "Windows"}]
        result = _build_intelligent_fallback(projects)
        assert "Waiting for Product" in result
        assert "cannot be scheduled" in result

    def test_priority_scheduled_over_cancelled(self):
        """Scheduled projects take priority in fallback messaging."""
        projects = [
            {"id": "1", "status": "Cancelled", "category": "Fencing"},
            {"id": "2", "status": "Scheduled", "category": "Roofing", "scheduledDate": "2026-04-01"},
        ]
        result = _build_intelligent_fallback(projects)
        data = json.loads(result)
        assert data["already_scheduled"] is True

    def test_generic_fallback(self):
        projects = [{"id": "1", "status": "In Progress", "category": "Decking"}]
        result = _build_intelligent_fallback(projects)
        assert "None are ready to schedule" in result


class TestMatchScheduledMonth:
    def test_matches(self):
        assert _match_scheduled_month("2026-03-15", "mar") is True

    def test_no_match(self):
        assert _match_scheduled_month("2026-03-15", "apr") is False

    def test_empty_date(self):
        assert _match_scheduled_month("", "mar") is False

    def test_invalid_date(self):
        assert _match_scheduled_month("not-a-date", "mar") is False


class TestMatchScheduledDate:
    def test_matches(self):
        assert _match_scheduled_date("2026-03-15", "2026-03-15") is True

    def test_no_match(self):
        assert _match_scheduled_date("2026-03-15", "2026-03-16") is False

    def test_empty_date(self):
        assert _match_scheduled_date("", "2026-03-15") is False

    def test_with_extra_time_info(self):
        assert _match_scheduled_date("2026-03-15T08:00:00", "2026-03-15") is True


class TestExtractProjectMinimalAddressId:
    def test_address_id_extracted(self):
        """address_id should be included in extracted project address."""
        item = {
            "project_project_id": "123",
            "project_project_number": "P-001",
            "status_info_status": "New",
            "project_installation_address_id": "777",
            "installation_address_address1": "123 Main St",
            "installation_address_city": "Springfield",
            "installation_address_state": "IL",
            "installation_address_zipcode": "62701",
            "store_info_store_name": "",
            "store_info_store_number": "",
        }
        result = _extract_project_minimal(item)
        assert result["address"]["address_id"] == "777"
        assert result["address"]["address1"] == "123 Main St"

    def test_address_id_missing(self):
        """When project_installation_address_id is absent, address_id is omitted."""
        item = {
            "project_project_id": "123",
            "status_info_status": "New",
            "installation_address_city": "Denver",
            "store_info_store_name": "",
            "store_info_store_number": "",
        }
        result = _extract_project_minimal(item)
        assert "address_id" not in result["address"]
        assert result["address"]["city"] == "Denver"


class TestGetInstallationAddress:
    async def test_returns_cached_address(self):
        """Returns address from project cache."""
        from datetime import datetime, timezone
        from tools.scheduling import _projects_cache

        _projects_cache["test-customer-456"] = {
            "projects": [
                {
                    "id": "123",
                    "status": "New",
                    "address": {"address_id": "777", "address1": "456 Oak Ave", "city": "Denver", "state": "CO", "zipcode": "80202"},
                }
            ],
            "loaded_at": datetime.now(timezone.utc),
        }

        result = await get_installation_address("123")
        data = json.loads(result)
        assert data["project_number"] == "123"
        assert data["address"]["city"] == "Denver"
        assert data["address"]["address_id"] == "777"

    async def test_cache_without_address(self):
        """Project in cache but no address returns error."""
        from datetime import datetime, timezone
        from tools.scheduling import _projects_cache

        _projects_cache["test-customer-456"] = {
            "projects": [
                {"id": "123", "status": "New", "address": {}},
            ],
            "loaded_at": datetime.now(timezone.utc),
        }

        result = await get_installation_address("123")
        assert "Could not retrieve" in result

    async def test_no_cache(self):
        """No cached project returns error string."""
        result = await get_installation_address("999")
        assert "Could not retrieve" in result


class TestUpdateInstallationAddress:
    async def test_returns_error_when_no_address_id(self, mock_httpx_client, mock_httpx_response):
        """Update returns error when no address_id can be found."""
        # get_installation_address returns no address_id
        response = mock_httpx_response(200, {"data": {}})
        with mock_httpx_client(response=response):
            result = await update_installation_address(
                "123", address1="123 New St", city="New City",
            )
        assert "cannot update address" in result.lower()
        assert "no address_id" in result.lower()

    async def test_sets_confirm_flag(self, mock_httpx_client, mock_httpx_response):
        """update_installation_address sets the confirm flag for guardrail."""
        from tools.scheduling import was_confirm_called, reset_confirm_flag

        reset_confirm_flag()
        response = mock_httpx_response(200, {"data": {}})
        with mock_httpx_client(response=response):
            await update_installation_address("123", address1="Test", city="City")
        assert was_confirm_called() is True
