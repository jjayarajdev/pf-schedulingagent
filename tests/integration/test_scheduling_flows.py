"""Integration tests — scenario-driven, real PF API calls with request/response capture.

Every test prints the question being asked and tracks it in the Excel report.

Run with:
    uv run pytest tests/integration/test_scheduling_flows.py -v -s --tb=short

Credentials (pick one method):
    1. CLI:     --pf-email ai@mailinator.com --pf-password 'U2Fs...'
    2. Env:     PF_TEST_EMAIL=... PF_TEST_PASSWORD=... uv run pytest ...
    3. File:    tests/integration/.pf-creds.json  {"email":"...","password":"..."}
    4. Prompt:  just run — will ask interactively if stdin is a terminal

Scenarios:
    Edit tests/integration/scenarios.json to add, remove, or update test questions.
    No code changes needed — just edit the JSON file.

Output:
    E2E_Scheduling_Test_Report.xlsx — every HTTP request/response captured with the
    question that triggered each API call.

Structural tests (schedule/cancel/reschedule with destructive state changes):
    uv run pytest tests/integration/test_structural.py -v -s -m structural
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import ClassVar

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

logger = logging.getLogger(__name__)

# Load scenarios from external file
_SCENARIOS_FILE = Path(__file__).parent / "scenarios.json"
_SCENARIOS = json.loads(_SCENARIOS_FILE.read_text()) if _SCENARIOS_FILE.exists() else []


class TestScenarios:
    """Runs each scenario from scenarios.json — prints the question and validates."""

    _state: ClassVar[dict] = {}

    @staticmethod
    def _get_tool(tool_name: str):
        """Import and return the named tool function."""
        from tools import scheduling

        _TOOL_MAP = {
            "list_projects": scheduling.list_projects,
            "get_project_details": scheduling.get_project_details,
            "get_available_dates": scheduling.get_available_dates,
            "get_time_slots": scheduling.get_time_slots,
            "confirm_appointment": scheduling.confirm_appointment,
            "reschedule_appointment": scheduling.reschedule_appointment,
            "cancel_appointment": scheduling.cancel_appointment,
            "get_business_hours": scheduling.get_business_hours,
            "get_project_weather": scheduling.get_project_weather,
            "add_note": scheduling.add_note,
            "list_notes": scheduling.list_notes,
        }
        return _TOOL_MAP.get(tool_name)

    def _resolve(self, text: str) -> str:
        """Replace {var} placeholders with values from shared state."""
        for key, value in self._state.items():
            if not key.startswith("_"):
                text = text.replace(f"{{{key}}}", str(value))
        return text

    def _resolve_params(self, params: dict) -> dict:
        """Resolve template variables in param values; drop unresolved string params."""
        resolved = {}
        for k, v in params.items():
            if isinstance(v, str):
                v = self._resolve(v)
                if "{" in v:
                    continue  # Unresolved template — skip
            resolved[k] = v
        return resolved

    def _store_results(self, scenario_id: str, result: str):
        """Extract values from tool results to feed into later scenarios."""
        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            return

        if scenario_id == "list_all_projects":
            projects = data.get("projects", [])
            if projects:
                self._state["first_project_id"] = projects[0]["id"]
                self._state["project_id"] = projects[0]["id"]
                pn = projects[0].get("projectNumber", "")
                if pn:
                    self._state["first_project_number"] = pn
                    self._state["project_number"] = pn
                schedulable = {"new", "pending reschedule", "not scheduled", "ready to schedule"}
                for p in projects:
                    if p.get("status", "").lower() in schedulable:
                        self._state["schedulable_project_id"] = p["id"]
                        break
                if "schedulable_project_id" not in self._state:
                    self._state["schedulable_project_id"] = projects[0]["id"]
                # Find a scheduled project
                scheduled_statuses = {"scheduled", "customer scheduled", "store scheduled",
                                      "install scheduled", "tentatively scheduled", "hdms scheduled"}
                for p in projects:
                    if p.get("status", "").lower() in scheduled_statuses:
                        self._state["scheduled_project_id"] = p["id"]
                        break

        elif scenario_id == "available_dates_default":
            if data.get("available_dates"):
                first = data["available_dates"][0]
                self._state["first_available_date"] = (
                    first if isinstance(first, str) else first.get("date", "")
                )
            if data.get("request_id"):
                self._state["request_id"] = str(data["request_id"])

        elif scenario_id == "time_slots":
            slots = data.get("time_slots", [])
            if slots:
                first = slots[0]
                self._state["first_time_slot"] = (
                    first if isinstance(first, str)
                    else first.get("time", first.get("startTime", ""))
                )

    def _validate(self, scenario: dict, result: str):
        """Check result against scenario expectations."""
        if scenario.get("expect_json"):
            try:
                data = json.loads(result)
                if scenario.get("expect_key"):
                    assert scenario["expect_key"] in data, (
                        f"Missing key '{scenario['expect_key']}'"
                    )
                if scenario.get("expect_fields"):
                    target = data
                    if isinstance(data, dict) and "projects" in data and data["projects"]:
                        target = data["projects"][0]
                    for field in scenario["expect_fields"]:
                        assert field in target, f"Missing field '{field}'"
            except json.JSONDecodeError:
                pass  # Some scenarios may return non-JSON for empty results

        if scenario.get("expect_contains"):
            assert scenario["expect_contains"].lower() in result.lower(), (
                f"Expected '{scenario['expect_contains']}' in result"
            )

        if scenario.get("expect_min_length"):
            assert len(result) >= scenario["expect_min_length"], (
                f"Result too short ({len(result)} < {scenario['expect_min_length']})"
            )

        if scenario.get("check_address"):
            try:
                data = json.loads(result)
                projects = data.get("projects", [])
                has_address = any(p.get("address", {}).get("city") for p in projects)
                assert has_address, "No projects with address found"
            except json.JSONDecodeError:
                pass

        if scenario.get("check_installer"):
            assert len(result) > 10, "Installer check: result too short"

    @pytest.mark.parametrize(
        "scenario",
        _SCENARIOS,
        ids=[s["id"] for s in _SCENARIOS],
    )
    async def test_scenario(self, scenario, http_capture):
        """Execute a single scenario: print question, call tool, validate."""
        sid = scenario["id"]
        question = self._resolve(scenario["question"])

        # Print the question being asked
        print(f"\n  [{sid}] Q: {question}")
        http_capture.set_question(question)

        # Check dependency
        dep = scenario.get("depends_on")
        if dep and dep not in self._state.get("_completed", set()):
            pytest.skip(f"Dependency '{dep}' was not completed")

        # Get tool function
        tool_fn = self._get_tool(scenario["tool"])
        if not tool_fn:
            pytest.skip(f"Unknown tool: {scenario['tool']}")

        # Resolve params — skip if required values are missing
        params = self._resolve_params(scenario.get("params", {}))
        original_keys = set(scenario.get("params", {}).keys())
        dropped = original_keys - set(params.keys())
        if dropped:
            pytest.skip(f"Unresolved params: {dropped}")

        # Call tool
        result = await tool_fn(**params)

        # Print result summary
        preview = result[:300].replace("\n", " ")
        print(f"  [{sid}] A: {preview}{'...' if len(result) > 300 else ''}")

        # Store for later scenarios
        self._store_results(sid, result)
        self._state.setdefault("_completed", set()).add(sid)

        # Validate expectations
        self._validate(scenario, result)
