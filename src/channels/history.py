"""Conversation history API — GET /conversations, GET /conversations/{session_id}."""

import logging
from collections import defaultdict

import boto3
from boto3.dynamodb.conditions import Attr, Key
from fastapi import APIRouter, HTTPException, Query

from config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _get_table():
    """Return the DynamoDB conversations table resource."""
    settings = get_settings()
    table_name = settings.dynamodb_conversations_table
    if not table_name:
        raise HTTPException(status_code=503, detail="Conversations table not configured")
    dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
    return dynamodb.Table(table_name)


@router.get(
    "/{session_id}",
    summary="Get full conversation for a session",
    description=(
        "Returns chronologically ordered messages with metadata for a given session. "
        "Each message includes role, text, agent name, channel, timestamp, and response time."
    ),
)
async def get_conversation(session_id: str):
    """Fetch all messages for a session from the conversations table."""
    try:
        table = _get_table()
        response = table.query(
            KeyConditionExpression=Key("session_id").eq(session_id),
            ScanIndexForward=True,
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to query conversation (session=%s)", session_id)
        raise HTTPException(status_code=500, detail="Failed to fetch conversation.") from None

    items = response.get("Items", [])
    messages = []
    for item in items:
        msg = {
            "role": item.get("role", ""),
            "message": item.get("message", ""),
            "agent_name": item.get("agent_name", ""),
            "channel": item.get("channel", ""),
            "created_at": item.get("created_at", ""),
        }
        if item.get("response_time_ms") is not None:
            msg["response_time_ms"] = int(item["response_time_ms"])
        if item.get("tools_called"):
            msg["tools_called"] = item["tools_called"]
        if item.get("intent"):
            msg["intent"] = item["intent"]
        messages.append(msg)

    return {
        "session_id": session_id,
        "message_count": len(messages),
        "messages": messages,
    }


@router.get(
    "",
    summary="List conversations",
    description=(
        "List conversation sessions with summary metadata. "
        "Filter by user_id (uses GSI), date range, or channel. "
        "Returns session summaries sorted by most recent activity."
    ),
)
async def list_conversations(
    user_id: str | None = Query(None, description="Filter by user_id (uses GSI)"),
    from_date: str | None = Query(None, description="Start date (ISO format, e.g. 2026-03-01T00:00:00)"),
    to_date: str | None = Query(None, description="End date (ISO format)"),
    channel: str | None = Query(None, description="Filter by channel (chat, vapi, sms)"),
    limit: int = Query(50, ge=1, le=200, description="Max sessions to return"),
):
    """List conversation sessions with summary metadata."""
    try:
        table = _get_table()

        if user_id:
            items = _query_by_user(table, user_id, from_date, to_date)
        else:
            items = _scan_conversations(table, from_date, to_date, channel)
    except HTTPException:
        raise
    except Exception:
        logger.exception("Failed to list conversations")
        raise HTTPException(status_code=500, detail="Failed to list conversations.") from None

    # Apply channel filter for GSI queries (GSI doesn't project channel as key)
    if user_id and channel:
        items = [i for i in items if i.get("channel") == channel]

    conversations = _group_into_sessions(items)
    conversations.sort(key=lambda c: c["last_message_at"], reverse=True)

    return {
        "conversations": conversations[:limit],
        "count": min(len(conversations), limit),
    }


def _query_by_user(table, user_id: str, from_date: str | None, to_date: str | None) -> list[dict]:
    """Query conversations by user_id using the GSI."""
    key_expr = Key("user_id").eq(user_id)
    if from_date and to_date:
        key_expr = key_expr & Key("created_at").between(from_date, to_date)
    elif from_date:
        key_expr = key_expr & Key("created_at").gte(from_date)
    elif to_date:
        key_expr = key_expr & Key("created_at").lte(to_date)

    response = table.query(
        IndexName="user-conversations-index",
        KeyConditionExpression=key_expr,
        ScanIndexForward=False,
    )
    return response.get("Items", [])


def _scan_conversations(table, from_date: str | None, to_date: str | None, channel: str | None) -> list[dict]:
    """Scan conversations with optional filters."""
    filter_expr = None

    if from_date:
        filter_expr = Attr("created_at").gte(from_date)
    if to_date:
        expr = Attr("created_at").lte(to_date)
        filter_expr = (filter_expr & expr) if filter_expr else expr
    if channel:
        expr = Attr("channel").eq(channel)
        filter_expr = (filter_expr & expr) if filter_expr else expr

    scan_kwargs = {}
    if filter_expr:
        scan_kwargs["FilterExpression"] = filter_expr

    response = table.scan(**scan_kwargs)
    return response.get("Items", [])


def _group_into_sessions(items: list[dict]) -> list[dict]:
    """Group DynamoDB items by session_id into conversation summaries."""
    sessions: dict[str, dict] = defaultdict(
        lambda: {
            "user_id": "",
            "client_id": "",
            "channel": "",
            "message_count": 0,
            "agents_used": set(),
            "first_message_at": "",
            "last_message_at": "",
        }
    )

    for item in items:
        sid = item.get("session_id", "")
        session = sessions[sid]
        session["user_id"] = item.get("user_id", "")
        session["client_id"] = item.get("client_id", "")
        session["channel"] = item.get("channel", "")
        session["message_count"] += 1

        created = item.get("created_at", "")
        if item.get("agent_name"):
            session["agents_used"].add(item["agent_name"])
        if not session["first_message_at"] or created < session["first_message_at"]:
            session["first_message_at"] = created
        if not session["last_message_at"] or created > session["last_message_at"]:
            session["last_message_at"] = created

    return [
        {
            "session_id": sid,
            "user_id": data["user_id"],
            "client_id": data["client_id"],
            "channel": data["channel"],
            "message_count": data["message_count"],
            "first_message_at": data["first_message_at"],
            "last_message_at": data["last_message_at"],
            "agents_used": sorted(data["agents_used"]),
        }
        for sid, data in sessions.items()
    ]
