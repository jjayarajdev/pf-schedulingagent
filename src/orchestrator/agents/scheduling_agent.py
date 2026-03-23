"""Scheduling Agent — handles all appointment scheduling operations."""

from datetime import datetime

from agent_squad.agents import BedrockLLMAgent, BedrockLLMAgentOptions
from agent_squad.utils.tool import AgentTool, AgentTools

from config import get_settings
from orchestrator.prompts.scheduling_agent import SCHEDULING_AGENT_PROMPT
from tools.scheduling import (
    add_note,
    cancel_appointment,
    confirm_appointment,
    get_available_dates,
    get_business_hours,
    get_project_details,
    get_project_weather,
    get_time_slots,
    list_notes,
    list_projects,
    reschedule_appointment,
)


def create_scheduling_agent() -> BedrockLLMAgent:
    """Create the Scheduling Agent with 10 tool bindings."""
    settings = get_settings()

    tools = [
        AgentTool(
            name="list_projects",
            description=(
                "List the customer's projects. Optionally filter by status, category, "
                "projectType, scheduled_month, or scheduled_date. "
                "Use status='schedulable' to show only projects that can be scheduled."
            ),
            properties={
                "status": {
                    "type": "string",
                    "description": "Filter by status (e.g. 'new', 'scheduled', 'schedulable' for all schedulable projects)",
                },
                "category": {"type": "string", "description": "Filter by project category (e.g. 'Roofing')"},
                "project_type": {"type": "string", "description": "Filter by project type (e.g. 'Windows Installation')"},
                "scheduled_month": {
                    "type": "string",
                    "description": "Filter by scheduled month name (e.g. 'January', 'March')",
                },
                "scheduled_date": {
                    "type": "string",
                    "description": "Filter by exact scheduled date (YYYY-MM-DD)",
                },
            },
            required=[],
            func=list_projects,
        ),
        AgentTool(
            name="get_project_details",
            description="Get detailed information about a specific project.",
            properties={
                "project_id": {"type": "string", "description": "The project ID"},
            },
            required=["project_id"],
            func=get_project_details,
        ),
        AgentTool(
            name="get_available_dates",
            description="Get available scheduling dates for a project.",
            properties={
                "project_id": {"type": "string", "description": "The project ID"},
                "start_date": {"type": "string", "description": "Start date (YYYY-MM-DD or natural language like 'next week')"},
                "end_date": {"type": "string", "description": "End date (YYYY-MM-DD)"},
            },
            required=["project_id"],
            func=get_available_dates,
        ),
        AgentTool(
            name="get_time_slots",
            description="Get available time slots for a specific date. Call get_available_dates first.",
            properties={
                "project_id": {"type": "string", "description": "The project ID"},
                "date": {"type": "string", "description": "The date (YYYY-MM-DD)"},
            },
            required=["project_id", "date"],
            func=get_time_slots,
        ),
        AgentTool(
            name="confirm_appointment",
            description=(
                "BOOKS the appointment in the system. You MUST call this tool to schedule — "
                "the appointment is NOT booked until this tool returns success. "
                "NEVER tell the user the appointment is confirmed without calling this tool first. "
                "Ask the user to confirm BEFORE calling this tool. Once they say yes, call it immediately."
            ),
            properties={
                "project_id": {"type": "string", "description": "The project ID"},
                "date": {"type": "string", "description": "The date (YYYY-MM-DD)"},
                "time": {"type": "string", "description": "The time slot (e.g. '08:00:00')"},
            },
            required=["project_id", "date", "time"],
            func=confirm_appointment,
        ),
        AgentTool(
            name="reschedule_appointment",
            description="REQUIRED to reschedule. Cancels existing appointment and prepares for new scheduling. You MUST call this tool — never tell the user it's done without calling it.",
            properties={
                "project_id": {"type": "string", "description": "The project ID"},
            },
            required=["project_id"],
            func=reschedule_appointment,
        ),
        AgentTool(
            name="cancel_appointment",
            description="REQUIRED to cancel. You MUST call this tool to cancel — never tell the user it's cancelled without calling it.",
            properties={
                "project_id": {"type": "string", "description": "The project ID"},
            },
            required=["project_id"],
            func=cancel_appointment,
        ),
        AgentTool(
            name="add_note",
            description="Add a note to a project.",
            properties={
                "project_id": {"type": "string", "description": "The project ID"},
                "note_text": {"type": "string", "description": "The note text to add"},
            },
            required=["project_id", "note_text"],
            func=add_note,
        ),
        AgentTool(
            name="list_notes",
            description="List all notes for a project.",
            properties={
                "project_id": {"type": "string", "description": "The project ID"},
            },
            required=["project_id"],
            func=list_notes,
        ),
        AgentTool(
            name="get_business_hours",
            description="Get business hours for the service provider.",
            properties={},
            required=[],
            func=get_business_hours,
        ),
        AgentTool(
            name="get_project_weather",
            description=(
                "Get weather forecast for a project's installation address. "
                "Use this when the user asks about weather for a specific project or "
                "asks 'what is the weather like' during a scheduling conversation."
            ),
            properties={
                "project_id": {
                    "type": "string",
                    "description": "The project ID. If omitted, uses the first project with an address.",
                },
            },
            required=[],
            func=get_project_weather,
        ),
    ]

    return BedrockLLMAgent(
        BedrockLLMAgentOptions(
            name="Scheduling Agent",
            description=(
                "Handles all appointment scheduling operations: viewing projects, checking available dates "
                "and times, scheduling appointments, rescheduling, cancelling, and managing project notes. "
                "Also handles weather queries when the user has been discussing projects — "
                "uses the project's installation address to get weather automatically. "
                "Routes here for ANY request when the conversation involves projects, scheduling, "
                "appointments, or follow-up questions about previously discussed projects including weather."
            ),
            model_id=settings.bedrock_model_id,
            region=settings.aws_region,
            streaming=False,
            tool_config={
                "tool": AgentTools(tools),
                "toolMaxRecursions": 8,
            },
            custom_system_prompt={
                "template": SCHEDULING_AGENT_PROMPT,
                "variables": {
                    "CURRENT_YEAR": str(datetime.now().year),
                },
            },
        )
    )
