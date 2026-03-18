"""Classifier routing tests — verify the Bedrock Classifier routes queries to the correct agent.

These tests call the full orchestrator pipeline (classifier → agent selection → response)
and verify that `response.metadata.agent_name` matches the expected agent for each query.

Run with:
    uv run pytest tests/integration/test_classifier_routing.py -v -s --tb=short

Scenarios are loaded from routing_scenarios.json — edit that file to add/modify test cases.
"""

import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import ClassVar

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

logger = logging.getLogger(__name__)

_SCENARIOS_FILE = Path(__file__).parent / "routing_scenarios.json"
_SCENARIOS = json.loads(_SCENARIOS_FILE.read_text()) if _SCENARIOS_FILE.exists() else []


class TestClassifierRouting:
    """Verify the Bedrock Classifier routes each query to the expected agent."""

    _results: ClassVar[list[dict]] = []

    @pytest.mark.parametrize(
        "scenario",
        _SCENARIOS,
        ids=[s["id"] for s in _SCENARIOS],
    )
    async def test_routing(self, scenario, http_capture):
        """Route a single query through the orchestrator and check the agent."""
        query = scenario["query"]
        expected_agent = scenario["expected_agent"]
        category = scenario["category"]

        print(f"\n  [{scenario['id']}] Q: {query}")
        print(f"  [{scenario['id']}] Expected: {expected_agent}")
        http_capture.set_question(query)

        from orchestrator import get_orchestrator
        from orchestrator.response_utils import extract_response_text

        orchestrator = get_orchestrator()

        # Use unique session per test to avoid history interference
        session_id = f"routing-test-{uuid.uuid4().hex[:8]}"
        user_id = "routing-test-user"

        try:
            response = await orchestrator.route_request(
                user_input=query,
                user_id=user_id,
                session_id=session_id,
                additional_params={"channel": "chat"},
            )
        except Exception as exc:
            pytest.fail(f"Orchestrator error for '{query}': {exc}")

        actual_agent = response.metadata.agent_name
        response_text = extract_response_text(response.output)
        preview = response_text[:200].replace("\n", " ")

        print(f"  [{scenario['id']}] Routed to: {actual_agent}")
        print(f"  [{scenario['id']}] Response: {preview}{'...' if len(response_text) > 200 else ''}")

        # Track result for summary
        self._results.append({
            "id": scenario["id"],
            "query": query,
            "expected": expected_agent,
            "actual": actual_agent,
            "match": actual_agent == expected_agent,
            "category": category,
        })

        assert actual_agent == expected_agent, (
            f"Routing mismatch for '{query}': "
            f"expected '{expected_agent}', got '{actual_agent}'"
        )

    @pytest.fixture(autouse=True, scope="class")
    def _print_summary(self):
        """Print a routing accuracy summary after all tests complete."""
        yield
        if not self._results:
            return

        total = len(self._results)
        correct = sum(1 for r in self._results if r["match"])
        accuracy = (correct / total * 100) if total else 0

        print(f"\n{'='*60}")
        print(f"  CLASSIFIER ROUTING SUMMARY")
        print(f"  Accuracy: {correct}/{total} ({accuracy:.1f}%)")
        print(f"{'='*60}")

        # Per-category breakdown
        categories = {}
        for r in self._results:
            cat = r["category"]
            if cat not in categories:
                categories[cat] = {"total": 0, "correct": 0}
            categories[cat]["total"] += 1
            if r["match"]:
                categories[cat]["correct"] += 1

        for cat, stats in sorted(categories.items()):
            cat_acc = (stats["correct"] / stats["total"] * 100) if stats["total"] else 0
            print(f"  {cat:15s}: {stats['correct']}/{stats['total']} ({cat_acc:.0f}%)")

        # List mismatches
        mismatches = [r for r in self._results if not r["match"]]
        if mismatches:
            print(f"\n  MISMATCHES:")
            for m in mismatches:
                print(f"    [{m['id']}] '{m['query']}'")
                print(f"      Expected: {m['expected']}  |  Got: {m['actual']}")
        print(f"{'='*60}")
