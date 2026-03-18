"""Chitchat Agent — greetings, help, small talk."""

from agent_squad.agents import BedrockLLMAgent, BedrockLLMAgentOptions

from config import get_settings
from orchestrator.prompts.chitchat_agent import CHITCHAT_AGENT_PROMPT


def create_chitchat_agent() -> BedrockLLMAgent:
    """Create the Chitchat Agent (no tools, LLM-only)."""
    settings = get_settings()

    return BedrockLLMAgent(
        BedrockLLMAgentOptions(
            name="Chitchat Agent",
            description=(
                "Handles greetings, casual conversation, help requests, and small talk. "
                "Routes here for 'hi', 'hello', 'help', 'what can you do', 'thanks', 'bye'."
            ),
            model_id=settings.bedrock_model_id,
            region=settings.aws_region,
            streaming=False,
            custom_system_prompt={
                "template": CHITCHAT_AGENT_PROMPT,
            },
        )
    )
