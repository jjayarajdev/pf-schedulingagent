"""Multi-turn orchestrator tests — verify AgentSquad maintains context across turns.

Tests full conversations through orchestrator.route_request(), confirming that:
  1. Session history is preserved between turns
  2. The agent can reference data from earlier turns
  3. The classifier routes correctly within an ongoing conversation
  4. Tool chains work across multiple exchanges (list → pick → dates → times)

Run with:
    uv run pytest tests/integration/test_multi_turn.py -v -s --tb=short
"""

import json
import logging
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

logger = logging.getLogger(__name__)


class TestMultiTurnSchedulingFlow:
    """Full scheduling conversation: list projects → pick one → get dates → get times."""

    async def test_full_scheduling_conversation(self, http_capture):
        """5-turn scheduling flow through the orchestrator."""
        from orchestrator import get_orchestrator
        from orchestrator.response_utils import extract_response_text

        orchestrator = get_orchestrator()
        session_id = f"multi-turn-{uuid.uuid4().hex[:8]}"
        user_id = "multi-turn-test-user"

        turns = [
            {
                "turn": 1,
                "message": "Show me all my projects",
                "checks": ["project", "status"],
                "description": "List all projects",
            },
            {
                "turn": 2,
                "message": "Which ones are ready to schedule?",
                "checks": [],
                "description": "Filter to schedulable projects (context-dependent)",
            },
            {
                "turn": 3,
                "message": "What dates are available for the first one?",
                "checks": [],
                "description": "Get available dates (references prior context)",
            },
            {
                "turn": 4,
                "message": "What time slots are available on the first date?",
                "checks": [],
                "description": "Get time slots (references date from prior turn)",
            },
            {
                "turn": 5,
                "message": "Thanks, I'll think about it",
                "checks": [],
                "description": "End conversation (should route to Chitchat or Scheduling)",
            },
        ]

        responses = []
        for turn in turns:
            question = turn["message"]
            print(f"\n  Turn {turn['turn']}: {question}")
            print(f"  ({turn['description']})")
            http_capture.set_question(question)

            try:
                response = await orchestrator.route_request(
                    user_input=question,
                    user_id=user_id,
                    session_id=session_id,
                    additional_params={"channel": "chat"},
                )
            except Exception as exc:
                pytest.fail(f"Turn {turn['turn']} failed: {exc}")

            response_text = extract_response_text(response.output)
            agent_name = response.metadata.agent_name
            preview = response_text[:300].replace("\n", " ")

            print(f"  → Agent: {agent_name}")
            print(f"  → Response: {preview}{'...' if len(response_text) > 300 else ''}")

            responses.append({
                "turn": turn["turn"],
                "message": question,
                "agent": agent_name,
                "response": response_text,
            })

            # First turn should route to Scheduling Agent
            if turn["turn"] == 1:
                assert agent_name == "Scheduling Agent", (
                    f"Turn 1 should route to Scheduling Agent, got '{agent_name}'"
                )

            # Check expected keywords in response
            for keyword in turn["checks"]:
                assert keyword.lower() in response_text.lower(), (
                    f"Turn {turn['turn']}: expected '{keyword}' in response"
                )

        # Verify we got responses for all turns
        assert len(responses) == len(turns), "Not all turns produced responses"

        # Print summary
        print(f"\n{'='*60}")
        print("  MULTI-TURN FLOW SUMMARY")
        for r in responses:
            print(f"  Turn {r['turn']}: [{r['agent']}] {r['message']}")
        print(f"{'='*60}")


class TestMultiTurnContextRetention:
    """Verify the orchestrator remembers context from earlier turns."""

    async def test_project_reference_across_turns(self, http_capture):
        """Ask about projects, then reference 'the first one' — agent should remember."""
        from orchestrator import get_orchestrator
        from orchestrator.response_utils import extract_response_text

        orchestrator = get_orchestrator()
        session_id = f"context-{uuid.uuid4().hex[:8]}"
        user_id = "context-test-user"

        # Turn 1: List projects
        http_capture.set_question("Show me my projects")
        print("\n  Turn 1: Show me my projects")
        resp1 = await orchestrator.route_request(
            user_input="Show me my projects",
            user_id=user_id,
            session_id=session_id,
            additional_params={"channel": "chat"},
        )
        text1 = extract_response_text(resp1.output)
        print(f"  → {text1[:200].replace(chr(10), ' ')}...")

        # Turn 2: Reference "the first one" — requires context from turn 1
        http_capture.set_question("Tell me more about the first project")
        print("\n  Turn 2: Tell me more about the first project")
        resp2 = await orchestrator.route_request(
            user_input="Tell me more about the first project",
            user_id=user_id,
            session_id=session_id,
            additional_params={"channel": "chat"},
        )
        text2 = extract_response_text(resp2.output)
        print(f"  → {text2[:200].replace(chr(10), ' ')}...")

        # The agent should have provided project-specific details, not asked "which project?"
        # At minimum, the response should be substantive
        assert len(text2) > 30, "Response too short — agent may have lost context"

        # Both turns should route to Scheduling Agent
        assert resp1.metadata.agent_name == "Scheduling Agent"
        assert resp2.metadata.agent_name == "Scheduling Agent"


class TestCrossCategoryConversation:
    """Test conversations that span multiple agent categories."""

    async def test_scheduling_then_chitchat_then_weather(self, http_capture):
        """User asks about projects, then says thanks, then asks about weather."""
        from orchestrator import get_orchestrator
        from orchestrator.response_utils import extract_response_text

        orchestrator = get_orchestrator()
        session_id = f"cross-cat-{uuid.uuid4().hex[:8]}"
        user_id = "cross-category-user"

        conversations = [
            ("What are my projects?", "Scheduling Agent"),
            ("Thanks for that info!", None),  # Could be Chitchat or Scheduling
            ("What's the weather in Miami?", "Weather Agent"),
        ]

        for i, (message, expected_agent) in enumerate(conversations, 1):
            print(f"\n  Turn {i}: {message}")
            http_capture.set_question(message)

            response = await orchestrator.route_request(
                user_input=message,
                user_id=user_id,
                session_id=session_id,
                additional_params={"channel": "chat"},
            )
            text = extract_response_text(response.output)
            agent = response.metadata.agent_name
            print(f"  → Agent: {agent}")
            print(f"  → Response: {text[:150].replace(chr(10), ' ')}...")

            if expected_agent:
                assert agent == expected_agent, (
                    f"Turn {i}: expected '{expected_agent}', got '{agent}'"
                )


class TestSessionIsolation:
    """Verify different sessions don't share context."""

    async def test_separate_sessions_no_bleed(self, http_capture):
        """Two sessions should not share conversation history."""
        from orchestrator import get_orchestrator
        from orchestrator.response_utils import extract_response_text

        orchestrator = get_orchestrator()
        session_a = f"session-a-{uuid.uuid4().hex[:8]}"
        session_b = f"session-b-{uuid.uuid4().hex[:8]}"
        user_id = "isolation-test-user"

        # Session A: talk about projects
        http_capture.set_question("Show me my projects")
        print("\n  Session A: Show me my projects")
        resp_a = await orchestrator.route_request(
            user_input="Show me my projects",
            user_id=user_id,
            session_id=session_a,
            additional_params={"channel": "chat"},
        )
        text_a = extract_response_text(resp_a.output)
        print(f"  → {text_a[:150].replace(chr(10), ' ')}...")

        # Session B: ask about weather (completely separate context)
        http_capture.set_question("What's the weather in New York?")
        print("\n  Session B: What's the weather in New York?")
        resp_b = await orchestrator.route_request(
            user_input="What's the weather in New York?",
            user_id=user_id,
            session_id=session_b,
            additional_params={"channel": "chat"},
        )
        text_b = extract_response_text(resp_b.output)
        agent_b = resp_b.metadata.agent_name
        print(f"  → Agent: {agent_b}")
        print(f"  → {text_b[:150].replace(chr(10), ' ')}...")

        # Session B should route to Weather Agent, not be influenced by Session A
        assert agent_b == "Weather Agent", (
            f"Session B should route to Weather Agent, got '{agent_b}'"
        )
