"""Weather Agent — provides weather forecasts."""

from agent_squad.agents import BedrockLLMAgent, BedrockLLMAgentOptions
from agent_squad.utils.tool import AgentTool, AgentTools

from config import get_settings
from orchestrator.prompts.weather_agent import WEATHER_AGENT_PROMPT
from tools.weather import get_weather


def create_weather_agent() -> BedrockLLMAgent:
    """Create the Weather Agent with get_weather tool."""
    settings = get_settings()

    weather_tool = AgentTool(
        name="get_weather",
        description=(
            "Get a 5-day weather forecast for a location. "
            "If location is omitted, automatically uses the installation address "
            "of the project the user was just discussing."
        ),
        properties={
            "location": {
                "type": "string",
                "description": "City, state, ZIP code, or address. Leave empty to use the current project's address.",
            },
        },
        required=[],
        func=get_weather,
    )

    return BedrockLLMAgent(
        BedrockLLMAgentOptions(
            name="Weather Agent",
            description=(
                "Provides weather forecasts ONLY for standalone location queries like "
                "'what is the weather in Miami' or 'forecast for 90210'. "
                "NEVER route here if the conversation involves projects, scheduling, or appointments — "
                "the Scheduling Agent handles all weather queries when projects have been discussed."
            ),
            model_id=settings.bedrock_model_id,
            region=settings.aws_region,
            streaming=False,
            tool_config={
                "tool": AgentTools([weather_tool]),
                "toolMaxRecursions": 2,
            },
            custom_system_prompt={
                "template": WEATHER_AGENT_PROMPT,
            },
        )
    )
