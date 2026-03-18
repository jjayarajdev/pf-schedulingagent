"""Welcome flow — personalized greeting with project summary.

When the frontend sends ``__WELCOME__`` as the first message after login,
this module fetches the user's projects and generates a casual, personalized
greeting via Bedrock Claude.  The greeting is stored in AgentSquad's
conversation history so subsequent messages retain context about which
projects the user has.
"""

import json
import logging
import time

import boto3

from config import get_settings
from tools.scheduling import _load_projects

logger = logging.getLogger(__name__)

# Bedrock client — lazily initialized
_bedrock_client = None


def _get_bedrock_client():
    global _bedrock_client  # noqa: PLW0603
    if _bedrock_client is None:
        settings = get_settings()
        _bedrock_client = boto3.client(
            "bedrock-runtime", region_name=settings.aws_region,
        )
    return _bedrock_client


def _build_project_summary(projects: list[dict]) -> str:
    """Build a readable project summary for the greeting prompt."""
    if not projects:
        return "No projects found"

    lines = []
    for p in projects:
        status = p.get("status", "Unknown")
        category = p.get("category", "Project")
        proj_id = p.get("id", "")
        scheduled_date = p.get("scheduledDate", "")

        if scheduled_date:
            lines.append(f"- {category} (#{proj_id}): {status}, scheduled for {scheduled_date}")
        else:
            lines.append(f"- {category} (#{proj_id}): {status}")

    return "\n".join(lines)


_GREETING_SYSTEM_PROMPT = """\
You're welcoming someone to their home services account.
Be warm and natural - like a friendly neighbor, not a corporate script.

GUIDELINES:
- Use their name if provided: "Hey John!" or "Hi Sarah!"
- No name? Just "Hey there!" or "Hi!"
- Mention what they have going on (projects, appointments)
- Keep it SHORT - 2-3 sentences max
- End with something helpful, not salesy

GOOD EXAMPLES:
"Hey John! Good to see you. You've got 3 projects going - your deck's \
ready to schedule whenever you are."
"Hi there! You have a roofing appointment coming up on the 26th. Need \
to make any changes?"
"Hey Sarah! Looks like your kitchen remodel is all scheduled. Anything \
else I can help with?"

AVOID:
- "Welcome back to ProjectForce" (too formal)
- "I'm here to assist you" (robotic)
- Long lists of everything they could do
- Multiple questions at the end\
"""


def _generate_greeting(user_name: str, projects: list[dict]) -> str:
    """Generate a personalized welcome greeting via Bedrock Claude.

    Falls back to a static greeting on any error.
    """
    project_data = _build_project_summary(projects)

    user_prompt = (
        f"Name: {user_name or 'none'}\n"
        f"Projects: {len(projects)}\n"
        f"Details:\n{project_data}\n\n"
        "Write a brief, friendly welcome. Be conversational."
    )

    try:
        client = _get_bedrock_client()
        settings = get_settings()
        response = client.converse(
            modelId=settings.bedrock_model_id,
            messages=[{"role": "user", "content": [{"text": user_prompt}]}],
            system=[{"text": _GREETING_SYSTEM_PROMPT}],
            inferenceConfig={"maxTokens": 200, "temperature": 0.7, "topP": 0.9},
        )
        greeting = response["output"]["message"]["content"][0]["text"]
        logger.info("Generated welcome greeting (%d chars)", len(greeting))
        return greeting.strip()

    except Exception:
        logger.exception("Failed to generate welcome greeting — using fallback")
        return _fallback_greeting(user_name, projects)


def _fallback_greeting(user_name: str, projects: list[dict]) -> str:
    """Static fallback when Bedrock is unavailable."""
    name_part = f" {user_name}" if user_name else ""
    if projects:
        categories = list({p.get("category", "project") for p in projects[:3]})
        types_str = ", ".join(categories[:2])
        return (
            f"Hey{name_part}! You've got {len(projects)} project(s) "
            f"with us — {types_str}. What can I help you with?"
        )
    return f"Hey{name_part}! Welcome. No projects set up yet, but I'm here when you're ready."


async def handle_welcome(user_name: str) -> dict:
    """Handle the ``__WELCOME__`` flow.

    1. Fetch user's projects (via the shared scheduling cache).
    2. Generate a personalized greeting.
    3. Return structured response for the chat endpoint.

    The caller is responsible for storing the response in conversation
    history (via AgentSquad's storage).
    """
    start = time.time()

    projects = await _load_projects()

    greeting = _generate_greeting(user_name, projects)

    # Append JSON block for frontend project table rendering
    if projects:
        result_data = {"message": f"Found {len(projects)} project(s):", "projects": projects}
        formatted = f"{greeting}\n\n```json\n{json.dumps(result_data, indent=2)}\n```"
    else:
        formatted = greeting

    elapsed = time.time() - start
    logger.info("Welcome flow completed in %.2fs (%d projects)", elapsed, len(projects))

    return {
        "response": formatted,
        "agent_name": "Welcome",
        "projects": projects,
    }
