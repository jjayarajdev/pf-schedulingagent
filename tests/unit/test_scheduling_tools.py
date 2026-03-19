"""Tests for scheduling tool handlers."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools.scheduling import (
    _build_intelligent_fallback,
    _get_reschedule_slots,
    _match_scheduled_date,
    _match_scheduled_month,
    add_note,
    cancel_appointment,
    confirm_appointment,
    get_available_dates,
    get_business_hours,
    get_project_details,
    get_time_slots,
    list_notes,
    list_projects,
    reschedule_appointment,
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


class TestGetTimeSlots:
    async def test_returns_slots(self, mock_httpx_client, mock_httpx_response):
        response = mock_httpx_response(
            200,
            {"slots": [{"time": "9:00 AM", "id": "slot-1"}, {"time": "10:00 AM", "id": "slot-2"}]},
        )
        with mock_httpx_client(response=response):
            result = await get_time_slots("123", "2026-03-15")
        data = json.loads(result)
        assert len(data["time_slots"]) == 2

    async def test_no_slots(self, mock_httpx_client, mock_httpx_response):
        response = mock_httpx_response(200, {"slots": []})
        with mock_httpx_client(response=response):
            result = await get_time_slots("123", "2026-03-15")
        assert "No time slots" in result


class TestConfirmAppointment:
    async def test_requires_confirmation(self):
        result = await confirm_appointment("123", "2026-03-15", "9:00 AM", confirmed=False)
        assert "confirm" in result.lower()

    async def test_confirmed_schedules(self, mock_httpx_client, mock_httpx_response):
        details_resp = mock_httpx_response(
            200,
            {"data": [{"project_project_id": "123", "status_info_status": "New", "store_info_store_name": "", "store_info_store_number": ""}]},
        )
        schedule_resp = mock_httpx_response(200, {"confirmationNumber": "CONF-789"})
        with mock_httpx_client(responses=[details_resp, schedule_resp]):
            result = await confirm_appointment("123", "2026-03-15", "9:00 AM", confirmed=True)
        assert "confirmed" in result.lower()
        assert "123" in result


class TestRescheduleAppointment:
    async def test_reschedule_with_rescheduler_api(self, mock_httpx_client, mock_httpx_response):
        """Reschedule uses dedicated rescheduler endpoint and returns new dates."""
        # Pre-populate cache with a scheduled project
        from tools.scheduling import _projects_cache
        from datetime import datetime, timezone

        _projects_cache["test-customer-456"] = {
            "projects": [
                {"id": "100", "status": "Scheduled", "scheduledDate": "2026-03-10",
                 "category": "Fencing", "address": {}},
            ],
            "loaded_at": datetime.now(timezone.utc),
        }

        cancel_resp = mock_httpx_response(200, {"status": "ok"})
        reschedule_resp = mock_httpx_response(200, {
            "data": {
                "dates": ["2026-03-20", "2026-03-21", "2026-03-22"],
                "request_id": 5555,
            }
        })

        with mock_httpx_client(responses=[cancel_resp, reschedule_resp]):
            result = await reschedule_appointment("100")

        data = json.loads(result)
        assert data["is_reschedule"] is True
        assert len(data["available_dates"]) == 3

    async def test_reschedule_fallback_when_api_fails(self, mock_httpx_client, mock_httpx_response):
        """Falls back to standard message when rescheduler API fails."""
        from tools.scheduling import _projects_cache
        from datetime import datetime, timezone

        _projects_cache["test-customer-456"] = {
            "projects": [
                {"id": "100", "status": "Scheduled", "scheduledDate": "2026-03-10",
                 "category": "Fencing", "address": {}},
            ],
            "loaded_at": datetime.now(timezone.utc),
        }

        cancel_resp = mock_httpx_response(200, {"status": "ok"})
        reschedule_resp = mock_httpx_response(500, None, text="Internal Server Error")

        with mock_httpx_client(responses=[cancel_resp, reschedule_resp]):
            result = await reschedule_appointment("100")

        assert "cancelled" in result.lower()
        assert "get_available_dates" in result


class TestAddNote:
    async def test_adds_note(self, mock_httpx_client, mock_httpx_response):
        response = mock_httpx_response(200, {"status": "ok"})
        with mock_httpx_client(response=response):
            result = await add_note("123", "Test note")
        assert "successfully" in result.lower()


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
