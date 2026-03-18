"""Tests for the conversation history API endpoints."""

import asyncio
from decimal import Decimal
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from config import Settings


def _create_conversations_table(region="us-east-1", table_name="pf-syn-schedulingagents-conversations-test"):
    """Create the conversations DynamoDB table in moto."""
    dynamodb = boto3.client("dynamodb", region_name=region)
    dynamodb.create_table(
        TableName=table_name,
        AttributeDefinitions=[
            {"AttributeName": "session_id", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "created_at", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "session_id", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "user-conversations-index",
                "KeySchema": [
                    {"AttributeName": "user_id", "KeyType": "HASH"},
                    {"AttributeName": "created_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return dynamodb


def _seed_conversations(table_name="pf-syn-schedulingagents-conversations-test"):
    """Insert sample conversation items for testing."""
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    table = dynamodb.Table(table_name)

    items = [
        {
            "session_id": "sess-1",
            "SK": "2026-03-15T10:00:00+00:00#0",
            "role": "user",
            "message": "When is my next appointment?",
            "user_id": "user-1",
            "client_id": "client-1",
            "channel": "chat",
            "agent_name": "Scheduling Agent",
            "created_at": "2026-03-15T10:00:00+00:00",
            "ttl": Decimal("9999999999"),
        },
        {
            "session_id": "sess-1",
            "SK": "2026-03-15T10:00:00+00:00#1",
            "role": "assistant",
            "message": "Your next appointment is on March 20 at 10:00 AM.",
            "user_id": "user-1",
            "client_id": "client-1",
            "channel": "chat",
            "agent_name": "Scheduling Agent",
            "created_at": "2026-03-15T10:00:00+00:00",
            "response_time_ms": Decimal("350"),
            "ttl": Decimal("9999999999"),
        },
        {
            "session_id": "sess-1",
            "SK": "2026-03-15T10:01:00+00:00#0",
            "role": "user",
            "message": "Can I reschedule?",
            "user_id": "user-1",
            "client_id": "client-1",
            "channel": "chat",
            "agent_name": "Scheduling Agent",
            "created_at": "2026-03-15T10:01:00+00:00",
            "ttl": Decimal("9999999999"),
        },
        {
            "session_id": "sess-1",
            "SK": "2026-03-15T10:01:00+00:00#1",
            "role": "assistant",
            "message": "Sure! Here are the available dates...",
            "user_id": "user-1",
            "client_id": "client-1",
            "channel": "chat",
            "agent_name": "Scheduling Agent",
            "created_at": "2026-03-15T10:01:00+00:00",
            "response_time_ms": Decimal("500"),
            "tools_called": ["get_available_dates"],
            "ttl": Decimal("9999999999"),
        },
        # Different session, same user, vapi channel
        {
            "session_id": "sess-2",
            "SK": "2026-03-16T08:00:00+00:00#0",
            "role": "user",
            "message": "What time is my appointment tomorrow?",
            "user_id": "user-1",
            "client_id": "client-1",
            "channel": "vapi",
            "agent_name": "Scheduling Agent",
            "created_at": "2026-03-16T08:00:00+00:00",
            "ttl": Decimal("9999999999"),
        },
        {
            "session_id": "sess-2",
            "SK": "2026-03-16T08:00:00+00:00#1",
            "role": "assistant",
            "message": "Your appointment is at 2 PM tomorrow.",
            "user_id": "user-1",
            "client_id": "client-1",
            "channel": "vapi",
            "agent_name": "Scheduling Agent",
            "created_at": "2026-03-16T08:00:00+00:00",
            "response_time_ms": Decimal("600"),
            "ttl": Decimal("9999999999"),
        },
    ]
    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)


@pytest.fixture()
def mock_settings():
    """Create a Settings instance for tests."""
    return Settings(
        environment="test",
        aws_region="us-east-1",
        dynamodb_conversations_table="pf-syn-schedulingagents-conversations-test",
    )


@pytest.fixture()
def _seeded_table():
    """Moto DynamoDB conversations table with sample data."""
    with mock_aws():
        _create_conversations_table()
        _seed_conversations()
        yield


def _run(coro):
    """Run an async function synchronously."""
    return asyncio.get_event_loop().run_until_complete(coro)


class TestGetConversation:
    """Tests for GET /conversations/{session_id}."""

    def test_returns_full_conversation(self, _seeded_table, mock_settings):
        """Returns all messages in chronological order."""
        with patch("channels.history.get_settings", return_value=mock_settings):
            from channels.history import get_conversation

            result = _run(get_conversation("sess-1"))

        assert result["session_id"] == "sess-1"
        assert result["message_count"] == 4
        messages = result["messages"]
        assert messages[0]["role"] == "user"
        assert messages[0]["message"] == "When is my next appointment?"
        assert messages[1]["role"] == "assistant"
        assert messages[1]["response_time_ms"] == 350
        assert messages[2]["role"] == "user"
        assert messages[3]["role"] == "assistant"

    def test_empty_session(self, _seeded_table, mock_settings):
        """Returns empty messages for non-existent session."""
        with patch("channels.history.get_settings", return_value=mock_settings):
            from channels.history import get_conversation

            result = _run(get_conversation("nonexistent"))

        assert result["session_id"] == "nonexistent"
        assert result["message_count"] == 0
        assert result["messages"] == []

    def test_includes_tools_called(self, _seeded_table, mock_settings):
        """Tools called field is included when present."""
        with patch("channels.history.get_settings", return_value=mock_settings):
            from channels.history import get_conversation

            result = _run(get_conversation("sess-1"))

        # The 4th message (index 3) has tools_called
        assistant_msgs = [m for m in result["messages"] if m["role"] == "assistant"]
        tools_msg = [m for m in assistant_msgs if "tools_called" in m][0]
        assert tools_msg["tools_called"] == ["get_available_dates"]


class TestListConversations:
    """Tests for GET /conversations — calls internal helpers directly."""

    def test_list_all(self, _seeded_table, mock_settings):
        """Lists all conversation sessions."""
        with patch("channels.history.get_settings", return_value=mock_settings):
            from channels.history import list_conversations

            result = _run(
                list_conversations(
                    user_id=None, from_date=None, to_date=None, channel=None, limit=50
                )
            )

        assert result["count"] == 2
        conversations = result["conversations"]
        # Most recent first
        assert conversations[0]["session_id"] == "sess-2"
        assert conversations[1]["session_id"] == "sess-1"

    def test_filter_by_user_id(self, _seeded_table, mock_settings):
        """Filters by user_id using GSI."""
        with patch("channels.history.get_settings", return_value=mock_settings):
            from channels.history import list_conversations

            result = _run(
                list_conversations(
                    user_id="user-1", from_date=None, to_date=None, channel=None, limit=50
                )
            )

        assert result["count"] == 2

    def test_filter_by_channel(self, _seeded_table, mock_settings):
        """Filters by channel."""
        with patch("channels.history.get_settings", return_value=mock_settings):
            from channels.history import list_conversations

            result = _run(
                list_conversations(
                    user_id=None, from_date=None, to_date=None, channel="vapi", limit=50
                )
            )

        assert result["count"] == 1
        assert result["conversations"][0]["channel"] == "vapi"

    def test_session_summary_fields(self, _seeded_table, mock_settings):
        """Session summaries include expected fields."""
        with patch("channels.history.get_settings", return_value=mock_settings):
            from channels.history import list_conversations

            result = _run(
                list_conversations(
                    user_id=None, from_date=None, to_date=None, channel=None, limit=50
                )
            )

        sess1 = [c for c in result["conversations"] if c["session_id"] == "sess-1"][0]
        assert sess1["user_id"] == "user-1"
        assert sess1["client_id"] == "client-1"
        assert sess1["channel"] == "chat"
        assert sess1["message_count"] == 4
        assert "Scheduling Agent" in sess1["agents_used"]
        assert sess1["first_message_at"]
        assert sess1["last_message_at"]

    def test_limit(self, _seeded_table, mock_settings):
        """Respects limit parameter."""
        with patch("channels.history.get_settings", return_value=mock_settings):
            from channels.history import list_conversations

            result = _run(
                list_conversations(
                    user_id=None, from_date=None, to_date=None, channel=None, limit=1
                )
            )

        assert result["count"] == 1

    def test_filter_by_date_range(self, _seeded_table, mock_settings):
        """Filters by from_date and to_date."""
        with patch("channels.history.get_settings", return_value=mock_settings):
            from channels.history import list_conversations

            result = _run(
                list_conversations(
                    user_id=None,
                    from_date="2026-03-16T00:00:00",
                    to_date="2026-03-17T00:00:00",
                    channel=None,
                    limit=50,
                )
            )

        assert result["count"] == 1
        assert result["conversations"][0]["session_id"] == "sess-2"

    def test_filter_by_user_and_channel(self, _seeded_table, mock_settings):
        """Combines user_id GSI query with channel filter."""
        with patch("channels.history.get_settings", return_value=mock_settings):
            from channels.history import list_conversations

            result = _run(
                list_conversations(
                    user_id="user-1", from_date=None, to_date=None, channel="chat", limit=50
                )
            )

        assert result["count"] == 1
        assert result["conversations"][0]["channel"] == "chat"
