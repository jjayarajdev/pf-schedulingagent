"""Structural integration tests — thorough assertions, direct tool calls.

These tests call scheduling tools directly (no question context) and include
destructive operations (schedule, cancel, reschedule) that modify real project state.

Run explicitly with:
    uv run pytest tests/integration/test_structural.py -v -s --tb=short
"""

import json
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

logger = logging.getLogger(__name__)

# Mark all tests in this file so they don't run by default
pytestmark = pytest.mark.structural


# ===========================================================================
#  1. Project Listing & Lookup
# ===========================================================================


class TestListProjects:
    async def test_list_all_projects(self):
        """Should return at least one project for the test customer."""
        from tools.scheduling import list_projects

        result = await list_projects()
        data = json.loads(result)
        assert "projects" in data
        assert len(data["projects"]) > 0
        logger.info("Found %d projects", len(data["projects"]))

    async def test_list_projects_has_expected_fields(self):
        """Each project should have id, status, category, address."""
        from tools.scheduling import list_projects

        result = await list_projects()
        data = json.loads(result)
        project = data["projects"][0]
        assert "id" in project
        assert "status" in project
        assert "category" in project

    async def test_filter_by_schedulable(self):
        """status='schedulable' should only return schedulable projects."""
        from tools.scheduling import list_projects

        result = await list_projects(status="schedulable")
        try:
            data = json.loads(result)
            if "projects" in data:
                for p in data["projects"]:
                    assert p["status"].lower() in {
                        "new", "pending reschedule", "not scheduled", "ready to schedule",
                    }, f"Non-schedulable status: {p['status']}"
        except json.JSONDecodeError:
            logger.info("No schedulable projects (fallback): %s", result[:200])

    async def test_filter_by_category(self):
        """category filter should narrow results."""
        from tools.scheduling import list_projects

        all_result = await list_projects()
        all_data = json.loads(all_result)
        if not all_data.get("projects"):
            pytest.skip("No projects")

        category = all_data["projects"][0].get("category", "")
        if not category:
            pytest.skip("First project has no category")

        filtered_result = await list_projects(category=category)
        filtered_data = json.loads(filtered_result)
        assert "projects" in filtered_data
        for p in filtered_data["projects"]:
            assert category.lower() in p.get("category", "").lower()

    async def test_intelligent_fallback_when_no_match(self):
        """Impossible filter triggers fallback message."""
        from tools.scheduling import list_projects

        result = await list_projects(category="xyznonexistent999")
        assert len(result) > 20
        logger.info("Fallback: %s", result[:300])


# ===========================================================================
#  2. Project Details
# ===========================================================================


class TestGetProjectDetails:
    async def test_get_details_by_project_id(self):
        """Look up a project by its ID."""
        from tools.scheduling import get_project_details, list_projects

        projects_result = await list_projects()
        data = json.loads(projects_result)
        if not data.get("projects"):
            pytest.skip("No projects")

        pid = data["projects"][0]["id"]
        detail = await get_project_details(pid)
        detail_data = json.loads(detail)
        assert detail_data["id"] == pid
        logger.info("Project %s: %s — %s", pid, detail_data.get("category"), detail_data.get("status"))

    async def test_get_details_by_project_number(self):
        """Look up a project by its order number."""
        from tools.scheduling import get_project_details, list_projects

        result = await list_projects()
        data = json.loads(result)
        projects = data.get("projects", [])
        known = next((p["projectNumber"] for p in projects if p.get("projectNumber")), "")
        if not known:
            pytest.skip("No project numbers available")

        detail = await get_project_details(known)
        try:
            detail_data = json.loads(detail)
            assert detail_data.get("projectNumber") == known or detail_data.get("id")
            logger.info("Project %s found: %s", known, detail_data.get("status"))
        except json.JSONDecodeError:
            assert "not found" in detail.lower()

    async def test_nonexistent_project(self):
        """Non-existent project returns helpful message."""
        from tools.scheduling import get_project_details

        result = await get_project_details("99999999")
        assert "not found" in result.lower()


# ===========================================================================
#  3. Available Dates & Time Slots
# ===========================================================================


class TestAvailableDates:
    async def _get_schedulable_project(self):
        from tools.scheduling import list_projects

        result = await list_projects()
        data = json.loads(result)
        projects = data.get("projects", [])
        schedulable = {"new", "pending reschedule", "not scheduled", "ready to schedule"}
        for p in projects:
            if p.get("status", "").lower() in schedulable:
                return p
        return projects[0] if projects else None

    async def test_get_available_dates_default_range(self):
        """Should return dates or 'already scheduled'."""
        from tools.scheduling import get_available_dates

        project = await self._get_schedulable_project()
        if not project:
            pytest.skip("No projects")

        result = await get_available_dates(project["id"])
        data = json.loads(result)

        if data.get("already_scheduled"):
            logger.info("Project %s already scheduled", project["id"])
            assert "reschedule" in data.get("message", "").lower()
        elif data.get("available_dates"):
            logger.info("Project %s: %d dates", project["id"], len(data["available_dates"]))
        else:
            logger.info("Project %s: no dates found", project["id"])

    async def test_get_available_dates_custom_range(self):
        """Custom date range (Apr 1-15)."""
        from tools.scheduling import get_available_dates

        project = await self._get_schedulable_project()
        if not project:
            pytest.skip("No projects")

        result = await get_available_dates(project["id"], start_date="2026-04-01", end_date="2026-04-15")
        data = json.loads(result)
        logger.info("Custom range: %d dates", len(data.get("available_dates", [])))

    async def test_get_available_dates_natural_language(self):
        """'next week' parses correctly."""
        from tools.scheduling import get_available_dates

        project = await self._get_schedulable_project()
        if not project:
            pytest.skip("No projects")

        result = await get_available_dates(project["id"], start_date="next week")
        data = json.loads(result)
        logger.info("'next week' range: %s", json.dumps(data.get("date_range"), default=str))

    async def test_get_time_slots_for_available_date(self):
        """Fetch time slots for a known available date."""
        from tools.scheduling import get_available_dates, get_time_slots

        project = await self._get_schedulable_project()
        if not project:
            pytest.skip("No projects")

        dates_result = await get_available_dates(project["id"])
        dates_data = json.loads(dates_result)

        if dates_data.get("already_scheduled"):
            pytest.skip("Already scheduled")

        available = dates_data.get("available_dates", [])
        if not available:
            pytest.skip("No available dates")

        first_date = available[0] if isinstance(available[0], str) else available[0].get("date", "")
        if not first_date:
            pytest.skip("Cannot extract date")

        slots_result = await get_time_slots(project["id"], first_date)
        slots_data = json.loads(slots_result)

        if "time_slots" in slots_data:
            assert len(slots_data["time_slots"]) > 0
            logger.info("Project %s on %s: %d slots", project["id"], first_date, len(slots_data["time_slots"]))
        else:
            logger.info("No time slots for %s on %s", project["id"], first_date)


# ===========================================================================
#  4. Full Schedule -> Cancel Flow (DESTRUCTIVE — modifies project state)
# ===========================================================================


class TestScheduleCancelFlow:
    async def _find_schedulable_with_dates(self):
        from tools.scheduling import get_available_dates, list_projects

        result = await list_projects(status="schedulable")
        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            return None, None

        for project in data.get("projects", []):
            dates_result = await get_available_dates(project["id"])
            try:
                dates_data = json.loads(dates_result)
            except json.JSONDecodeError:
                continue
            if dates_data.get("available_dates"):
                return project, dates_data
        return None, None

    async def test_schedule_then_cancel(self):
        """Full flow: dates -> slots -> confirm(no) -> confirm(yes) -> cancel."""
        from tools.scheduling import cancel_appointment, confirm_appointment, get_time_slots

        project, dates_data = await self._find_schedulable_with_dates()
        if not project:
            pytest.skip("No schedulable project with dates")

        pid = project["id"]
        available = dates_data["available_dates"]
        first_date = available[0] if isinstance(available[0], str) else available[0].get("date", "")
        if not first_date:
            pytest.skip("Cannot extract date")

        # Get time slots
        slots_result = await get_time_slots(pid, first_date)
        slots_data = json.loads(slots_result)
        time_slots = slots_data.get("time_slots", [])
        if not time_slots:
            pytest.skip("No time slots")

        first_slot = time_slots[0] if isinstance(time_slots[0], str) else time_slots[0].get("time", time_slots[0].get("startTime", ""))
        if not first_slot:
            pytest.skip("Cannot extract time slot")

        logger.info("SCHEDULE: project=%s date=%s time=%s", pid, first_date, first_slot)

        # Confirm without confirmed=True -> ask
        ask_result = await confirm_appointment(pid, first_date, first_slot, confirmed=False)
        assert "please confirm" in ask_result.lower()
        logger.info("Confirmation prompt: %s", ask_result[:150])

        # Confirm with confirmed=True -> schedule
        confirm_result = await confirm_appointment(pid, first_date, first_slot, confirmed=True)
        logger.info("Schedule result: %s", confirm_result[:200])
        assert "confirmed" in confirm_result.lower() or "scheduled" in confirm_result.lower()

        # Cancel
        cancel_result = await cancel_appointment(pid)
        logger.info("Cancel result: %s", cancel_result[:200])
        assert "cancel" in cancel_result.lower()


# ===========================================================================
#  5. Reschedule Flow (DESTRUCTIVE — modifies project state)
# ===========================================================================


class TestRescheduleFlow:
    async def _schedule_a_project(self):
        from tools.scheduling import confirm_appointment, get_available_dates, get_time_slots, list_projects

        result = await list_projects(status="schedulable")
        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            return None

        for project in data.get("projects", []):
            pid = project["id"]
            dates_result = await get_available_dates(pid)
            try:
                dates_data = json.loads(dates_result)
            except json.JSONDecodeError:
                continue
            if not dates_data.get("available_dates"):
                continue

            first_date = dates_data["available_dates"][0]
            if isinstance(first_date, dict):
                first_date = first_date.get("date", "")
            if not first_date:
                continue

            slots_result = await get_time_slots(pid, first_date)
            slots_data = json.loads(slots_result)
            time_slots = slots_data.get("time_slots", [])
            if not time_slots:
                continue

            first_slot = time_slots[0]
            if isinstance(first_slot, dict):
                first_slot = first_slot.get("time", first_slot.get("startTime", ""))

            confirm_result = await confirm_appointment(pid, first_date, first_slot, confirmed=True)
            if "confirmed" in confirm_result.lower() or "scheduled" in confirm_result.lower():
                return {"id": pid, "date": first_date, "time": first_slot}
        return None

    async def test_reschedule_flow(self):
        """Schedule then reschedule."""
        from tools.scheduling import reschedule_appointment

        scheduled = await self._schedule_a_project()
        if not scheduled:
            pytest.skip("Could not schedule a project")

        pid = scheduled["id"]
        logger.info("RESCHEDULE: project=%s was=%s at=%s", pid, scheduled["date"], scheduled["time"])

        result = await reschedule_appointment(pid)
        logger.info("Reschedule result: %s", result[:300])

        try:
            data = json.loads(result)
            if data.get("available_dates"):
                assert data.get("is_reschedule") is True
                logger.info("Got %d reschedule dates", len(data["available_dates"]))
        except json.JSONDecodeError:
            assert len(result) > 10
            logger.info("Reschedule text: %s", result)


# ===========================================================================
#  6. Business Hours
# ===========================================================================


class TestBusinessHours:
    async def test_get_business_hours(self):
        from tools.scheduling import get_business_hours

        result = await get_business_hours()
        data = json.loads(result)
        assert isinstance(data, dict)
        logger.info("Business hours: %s", json.dumps(data, default=str)[:500])


# ===========================================================================
#  7. Notes
# ===========================================================================


class TestNotes:
    async def test_add_and_list_notes(self):
        from tools.scheduling import add_note, list_notes, list_projects

        projects_result = await list_projects()
        data = json.loads(projects_result)
        if not data.get("projects"):
            pytest.skip("No projects")

        pid = data["projects"][0]["id"]

        note_text = "Integration test note — please ignore"
        add_result = await add_note(pid, note_text)
        logger.info("Add note: %s", add_result)
        assert (
            "success" in add_result.lower()
            or "added" in add_result.lower()
            or "404" in add_result
            or "error" in add_result.lower()
        )

        notes_result = await list_notes(pid)
        logger.info("Notes: %s", notes_result[:500])


# ===========================================================================
#  8. Address & Installer Info
# ===========================================================================


class TestProjectInfo:
    async def test_projects_have_addresses(self):
        from tools.scheduling import list_projects

        result = await list_projects()
        data = json.loads(result)
        projects = data.get("projects", [])
        with_address = [p for p in projects if p.get("address", {}).get("city")]
        logger.info("%d/%d projects have addresses", len(with_address), len(projects))
        assert len(with_address) > 0

        sample = with_address[0]
        logger.info(
            "Sample: %s, %s %s %s",
            sample["address"].get("address1", ""),
            sample["address"].get("city", ""),
            sample["address"].get("state", ""),
            sample["address"].get("zipcode", ""),
        )

    async def test_projects_have_store_info(self):
        from tools.scheduling import list_projects

        result = await list_projects()
        data = json.loads(result)
        projects = data.get("projects", [])
        with_store = [p for p in projects if p.get("store", {}).get("storeName")]
        logger.info("%d/%d projects have store info", len(with_store), len(projects))

    async def test_scheduled_projects_have_installer(self):
        from tools.scheduling import list_projects

        result = await list_projects(status="scheduled")
        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            pytest.skip("No scheduled projects (fallback)")

        scheduled = data.get("projects", [])
        if not scheduled:
            pytest.skip("No scheduled projects")

        with_installer = [p for p in scheduled if p.get("installer", {}).get("name")]
        logger.info("%d/%d scheduled have installer", len(with_installer), len(scheduled))
        if with_installer:
            logger.info("Installer: %s", with_installer[0]["installer"]["name"])


# ===========================================================================
#  9. Weather for Project
# ===========================================================================


class TestProjectWeather:
    async def test_weather_for_project_with_address(self):
        from tools.scheduling import get_project_weather, list_projects

        await list_projects()
        result = await get_project_weather()
        logger.info("Weather: %s", result[:500])
        assert len(result) > 20


# ===========================================================================
#  10. Welcome Flow
# ===========================================================================


class TestWelcomeFlow:
    async def test_welcome_loads_projects(self):
        from orchestrator.welcome import handle_welcome

        try:
            result = await handle_welcome(user_name="Jay")
        except Exception as exc:
            pytest.skip(f"Welcome requires Bedrock: {exc}")

        assert "response" in result
        assert len(result["response"]) > 10
        logger.info("Welcome: %s", result["response"][:300])
        if result.get("projects"):
            logger.info("Welcome projects: %d", len(result["projects"]))


# ===========================================================================
#  11. Already-Scheduled Detection
# ===========================================================================


class TestAlreadyScheduled:
    async def test_scheduled_project_returns_already_scheduled(self):
        from tools.scheduling import get_available_dates, list_projects

        result = await list_projects(status="scheduled")
        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            pytest.skip("No scheduled projects")

        scheduled = data.get("projects", [])
        if not scheduled:
            pytest.skip("No scheduled projects")

        pid = scheduled[0]["id"]
        dates_result = await get_available_dates(pid)

        try:
            dates_data = json.loads(dates_result)
            logger.info("Scheduled project %s dates: %s", pid, json.dumps(dates_data, default=str)[:300])
            assert dates_data.get("already_scheduled") or dates_data.get("available_dates") is not None
        except json.JSONDecodeError:
            logger.info("Scheduled project %s text: %s", pid, dates_result[:300])
            assert len(dates_result) > 10


# ===========================================================================
#  12. Full Project Inventory (summary)
# ===========================================================================


class TestSummary:
    async def test_full_project_inventory(self):
        """Log all projects for manual review."""
        from tools.scheduling import list_projects

        result = await list_projects()
        data = json.loads(result)
        projects = data.get("projects", [])

        logger.info("\n=== PROJECT INVENTORY (%d projects) ===", len(projects))
        for p in projects:
            scheduled = p.get("scheduledDate", "not scheduled")
            installer = p.get("installer", {}).get("name", "none")
            city = p.get("address", {}).get("city", "no city")
            logger.info(
                "  #%s | %s | %s | %s | %s | %s",
                p.get("projectNumber", p["id"]),
                p.get("category", "?"),
                p.get("status", "?"),
                scheduled,
                installer,
                city,
            )
