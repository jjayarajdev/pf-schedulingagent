"""Tests for the welcome flow."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.welcome import (
    _build_project_summary,
    _fallback_greeting,
    _generate_greeting,
    handle_welcome,
)


class TestBuildProjectSummary:
    def test_no_projects(self):
        assert _build_project_summary([]) == "No projects found"

    def test_single_project(self):
        projects = [{"id": "100", "status": "New", "category": "Roofing"}]
        result = _build_project_summary(projects)
        assert "Roofing (#100)" in result
        assert "New" in result

    def test_scheduled_project_includes_date(self):
        projects = [
            {"id": "200", "status": "Scheduled", "category": "Fencing", "scheduledDate": "2026-03-20"},
        ]
        result = _build_project_summary(projects)
        assert "scheduled for 2026-03-20" in result

    def test_multiple_projects(self):
        projects = [
            {"id": "1", "status": "New", "category": "Decking"},
            {"id": "2", "status": "Scheduled", "category": "Windows", "scheduledDate": "2026-04-01"},
        ]
        result = _build_project_summary(projects)
        lines = result.strip().split("\n")
        assert len(lines) == 2


class TestFallbackGreeting:
    def test_with_name_and_projects(self):
        projects = [{"category": "Roofing"}, {"category": "Fencing"}]
        result = _fallback_greeting("John", projects)
        assert "John" in result
        assert "2 project(s)" in result

    def test_without_name(self):
        result = _fallback_greeting("", [{"category": "Decking"}])
        assert "Hey!" in result
        assert "1 project(s)" in result

    def test_no_projects(self):
        result = _fallback_greeting("Sarah", [])
        assert "Sarah" in result
        assert "No projects" in result


class TestGenerateGreeting:
    @patch("orchestrator.welcome._get_bedrock_client")
    def test_bedrock_success(self, mock_client_fn):
        mock_client = MagicMock()
        mock_client.converse.return_value = {
            "output": {"message": {"content": [{"text": "Hey John! Your deck project is ready."}]}},
        }
        mock_client_fn.return_value = mock_client

        result = _generate_greeting("John", [{"id": "1", "status": "New", "category": "Decking"}])
        assert "Hey John" in result
        mock_client.converse.assert_called_once()

    @patch("orchestrator.welcome._get_bedrock_client")
    def test_bedrock_failure_falls_back(self, mock_client_fn):
        mock_client = MagicMock()
        mock_client.converse.side_effect = RuntimeError("Bedrock error")
        mock_client_fn.return_value = mock_client

        result = _generate_greeting("Sarah", [{"category": "Roofing"}])
        # Should use fallback
        assert "Sarah" in result
        assert "1 project(s)" in result


class TestHandleWelcome:
    @pytest.mark.asyncio
    @patch("orchestrator.welcome._generate_greeting")
    @patch("orchestrator.welcome._load_projects", new_callable=AsyncMock)
    async def test_returns_greeting_with_projects(self, mock_load, mock_greet):
        mock_load.return_value = [
            {"id": "100", "status": "New", "category": "Roofing"},
            {"id": "200", "status": "Scheduled", "category": "Fencing"},
        ]
        mock_greet.return_value = "Hey! You've got 2 projects."

        result = await handle_welcome(user_name="Test")

        assert result["agent_name"] == "Welcome"
        assert "Hey!" in result["response"]
        assert "```json" in result["response"]
        assert len(result["projects"]) == 2

    @pytest.mark.asyncio
    @patch("orchestrator.welcome._generate_greeting")
    @patch("orchestrator.welcome._load_projects", new_callable=AsyncMock)
    async def test_no_projects_no_json_block(self, mock_load, mock_greet):
        mock_load.return_value = []
        mock_greet.return_value = "Hey! No projects yet."

        result = await handle_welcome(user_name="")

        assert "```json" not in result["response"]
        assert result["projects"] == []

    @pytest.mark.asyncio
    @patch("orchestrator.welcome._generate_greeting")
    @patch("orchestrator.welcome._load_projects", new_callable=AsyncMock)
    async def test_passes_user_name(self, mock_load, mock_greet):
        mock_load.return_value = []
        mock_greet.return_value = "Hey Jane!"

        await handle_welcome(user_name="Jane")
        mock_greet.assert_called_once_with("Jane", [])
