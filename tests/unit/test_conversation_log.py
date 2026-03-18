"""Tests for the conversation logging module."""

import time
from decimal import Decimal
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from auth.context import AuthContext
from channels.conversation_log import log_conversation


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


@pytest.fixture()
def _conversations_table():
    """Moto DynamoDB conversations table."""
    with mock_aws():
        _create_conversations_table()
        yield


class TestLogConversation:
    """Tests for the log_conversation function."""

    @pytest.mark.asyncio
    async def test_writes_two_items(self, _conversations_table):
        """Writes a user + assistant message pair."""
        AuthContext.set(client_id="client-123")
        await log_conversation(
            session_id="sess-1",
            user_id="user-1",
            user_message="When is my appointment?",
            bot_response="Your appointment is scheduled for March 20.",
            agent_name="Scheduling Agent",
            channel="chat",
            response_time_ms=450,
        )

        # Verify items in DynamoDB
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.Table("pf-syn-schedulingagents-conversations-test")
        result = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("session_id").eq("sess-1"),
        )
        items = result["Items"]
        assert len(items) == 2

        user_item = [i for i in items if i["role"] == "user"][0]
        assert user_item["message"] == "When is my appointment?"
        assert user_item["session_id"] == "sess-1"
        assert user_item["user_id"] == "user-1"
        assert user_item["client_id"] == "client-123"
        assert user_item["channel"] == "chat"
        assert user_item["agent_name"] == "Scheduling Agent"
        assert user_item["SK"].endswith("#0")

        assistant_item = [i for i in items if i["role"] == "assistant"][0]
        assert assistant_item["message"] == "Your appointment is scheduled for March 20."
        assert assistant_item["response_time_ms"] == Decimal("450")
        assert assistant_item["SK"].endswith("#1")

    @pytest.mark.asyncio
    async def test_includes_optional_fields(self, _conversations_table):
        """Intent and tools_called are written when provided."""
        AuthContext.set(client_id="c1")
        await log_conversation(
            session_id="sess-2",
            user_id="user-2",
            user_message="Schedule for next Monday",
            bot_response="Appointment confirmed.",
            agent_name="Scheduling Agent",
            channel="chat",
            response_time_ms=800,
            intent="scheduling",
            tools_called=["confirm_appointment"],
        )

        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.Table("pf-syn-schedulingagents-conversations-test")
        result = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("session_id").eq("sess-2"),
        )
        items = result["Items"]
        assistant_item = [i for i in items if i["role"] == "assistant"][0]
        assert assistant_item["intent"] == "scheduling"
        assert assistant_item["tools_called"] == ["confirm_appointment"]

    @pytest.mark.asyncio
    async def test_ttl_set(self, _conversations_table):
        """Items have a TTL attribute set ~90 days in the future."""
        AuthContext.set(client_id="")
        await log_conversation(
            session_id="sess-ttl",
            user_id="u",
            user_message="q",
            bot_response="a",
            agent_name="Scheduling Agent",
            channel="chat",
            response_time_ms=100,
        )

        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.Table("pf-syn-schedulingagents-conversations-test")
        result = table.query(
            KeyConditionExpression=boto3.dynamodb.conditions.Key("session_id").eq("sess-ttl"),
        )
        item = result["Items"][0]
        ttl_val = int(item["ttl"])
        now = int(time.time())
        # TTL should be ~90 days from now (allow 1 day tolerance)
        assert 89 * 86400 < (ttl_val - now) < 91 * 86400

    @pytest.mark.asyncio
    async def test_empty_table_name_skips(self):
        """No write when conversations_table is empty."""
        with patch("channels.conversation_log.get_settings") as mock_gs:
            mock_gs.return_value.dynamodb_conversations_table = ""
            # Should not raise
            await log_conversation(
                session_id="s",
                user_id="u",
                user_message="q",
                bot_response="a",
                agent_name="Agent",
                channel="chat",
                response_time_ms=0,
            )

    @pytest.mark.asyncio
    async def test_exception_caught_silently(self):
        """DynamoDB errors are logged but never raised."""
        with patch("channels.conversation_log.boto3") as mock_boto:
            mock_boto.resource.side_effect = RuntimeError("boom")
            # Should not raise
            await log_conversation(
                session_id="s",
                user_id="u",
                user_message="q",
                bot_response="a",
                agent_name="Agent",
                channel="chat",
                response_time_ms=0,
            )
