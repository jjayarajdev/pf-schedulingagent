"""Conversation logging — append-only DynamoDB log of all user/assistant messages.

Fire-and-forget: catches all exceptions, never blocks the response path.
Each call writes two items (user message + assistant response) via batch_writer.
"""

import logging
from datetime import UTC, datetime

import boto3

from auth.context import AuthContext
from config import get_settings

logger = logging.getLogger(__name__)

_TTL_SECONDS = 90 * 24 * 60 * 60  # 90-day retention


async def log_conversation(
    session_id: str,
    user_id: str,
    user_message: str,
    bot_response: str,
    agent_name: str,
    channel: str,
    response_time_ms: int,
    intent: str = "",
    tools_called: list[str] | None = None,
) -> None:
    """Write user + assistant message pair to the conversations table.

    Fire-and-forget — catches all exceptions and never raises.
    """
    try:
        settings = get_settings()
        table_name = settings.dynamodb_conversations_table
        if not table_name:
            return

        now = datetime.now(UTC)
        iso_ts = now.isoformat()
        ttl = int(now.timestamp()) + _TTL_SECONDS
        client_id = AuthContext.get_client_id()

        common = {
            "session_id": session_id,
            "user_id": user_id,
            "client_id": client_id,
            "channel": channel,
            "agent_name": agent_name,
            "created_at": iso_ts,
            "ttl": ttl,
        }
        if intent:
            common["intent"] = intent

        user_item = {
            **common,
            "SK": f"{iso_ts}#0",
            "role": "user",
            "message": user_message,
        }

        assistant_item = {
            **common,
            "SK": f"{iso_ts}#1",
            "role": "assistant",
            "message": bot_response,
            "response_time_ms": response_time_ms,
        }
        if tools_called:
            assistant_item["tools_called"] = tools_called

        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        table = dynamodb.Table(table_name)
        with table.batch_writer() as batch:
            batch.put_item(Item=user_item)
            batch.put_item(Item=assistant_item)

        logger.debug(
            "Logged conversation: session=%s channel=%s agent=%s",
            session_id,
            channel,
            agent_name,
        )
    except Exception:
        logger.exception("Failed to log conversation (session=%s)", session_id)
