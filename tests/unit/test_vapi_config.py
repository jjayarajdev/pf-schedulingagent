"""Tests for the Vapi assistant configuration module."""

from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from config import Settings


def _create_vapi_table(region="us-east-1", table_name="pf-syn-schedulingagents-vapi-assistants-test"):
    """Create the vapi assistants DynamoDB table in moto."""
    dynamodb = boto3.client("dynamodb", region_name=region)
    dynamodb.create_table(
        TableName=table_name,
        AttributeDefinitions=[
            {"AttributeName": "assistant_id", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "assistant_id", "KeyType": "HASH"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return dynamodb


@pytest.fixture()
def mock_settings():
    """Create a Settings instance for tests."""
    return Settings(
        environment="test",
        aws_region="us-east-1",
        vapi_assistants_table="pf-syn-schedulingagents-vapi-assistants-test",
    )


@pytest.fixture()
def _vapi_table():
    """Moto DynamoDB vapi assistants table."""
    with mock_aws():
        _create_vapi_table()
        yield


class TestGetPhoneForAssistant:
    """Tests for get_phone_for_assistant."""

    def test_returns_empty_for_unknown(self, _vapi_table, mock_settings):
        """Returns empty string for unregistered assistant."""
        with patch("channels.vapi_config.get_settings", return_value=mock_settings):
            from channels.vapi_config import _cache, get_phone_for_assistant

            _cache.clear()
            result = get_phone_for_assistant("unknown-id")

        assert result == ""

    def test_returns_empty_for_empty_id(self, mock_settings):
        """Returns empty string for empty assistant ID."""
        with patch("channels.vapi_config.get_settings", return_value=mock_settings):
            from channels.vapi_config import get_phone_for_assistant

            result = get_phone_for_assistant("")

        assert result == ""

    def test_returns_phone_after_register(self, _vapi_table, mock_settings):
        """Returns phone number for a registered assistant."""
        with patch("channels.vapi_config.get_settings", return_value=mock_settings):
            from channels.vapi_config import _cache, get_phone_for_assistant, register_assistant

            _cache.clear()
            register_assistant("asst-1", "+19566699322", "Test Tenant")
            _cache.clear()  # Force DynamoDB lookup
            result = get_phone_for_assistant("asst-1")

        assert result == "+19566699322"

    def test_uses_cache(self, _vapi_table, mock_settings):
        """Subsequent lookups use in-memory cache."""
        with patch("channels.vapi_config.get_settings", return_value=mock_settings):
            from channels.vapi_config import _cache, get_phone_for_assistant, register_assistant

            _cache.clear()
            register_assistant("asst-2", "+15551234567", "Cached Tenant")
            _cache.clear()

            # First call: DynamoDB
            result1 = get_phone_for_assistant("asst-2")
            # Second call: cache (even if DynamoDB is gone)
            result2 = get_phone_for_assistant("asst-2")

        assert result1 == "+15551234567"
        assert result2 == "+15551234567"
        assert "asst-2" in _cache

    def test_returns_empty_when_table_not_configured(self):
        """Returns empty string when table name is empty."""
        settings = Settings(environment="test", aws_region="us-east-1", vapi_assistants_table="")
        with patch("channels.vapi_config.get_settings", return_value=settings):
            from channels.vapi_config import _cache, get_phone_for_assistant

            _cache.clear()
            result = get_phone_for_assistant("asst-x")

        assert result == ""


class TestRegisterAssistant:
    """Tests for register_assistant."""

    def test_creates_item(self, _vapi_table, mock_settings):
        """Stores assistant config in DynamoDB."""
        with patch("channels.vapi_config.get_settings", return_value=mock_settings):
            from channels.vapi_config import register_assistant

            register_assistant("asst-new", "+18005551234", "New Corp")

        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.Table("pf-syn-schedulingagents-vapi-assistants-test")
        item = table.get_item(Key={"assistant_id": "asst-new"})["Item"]
        assert item["phone_number"] == "+18005551234"
        assert item["tenant_name"] == "New Corp"
        assert "created_at" in item
        assert "updated_at" in item

    def test_updates_existing(self, _vapi_table, mock_settings):
        """Overwrites existing assistant config."""
        with patch("channels.vapi_config.get_settings", return_value=mock_settings):
            from channels.vapi_config import register_assistant

            register_assistant("asst-up", "+11111111111", "Old")
            register_assistant("asst-up", "+12222222222", "New")

        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.Table("pf-syn-schedulingagents-vapi-assistants-test")
        item = table.get_item(Key={"assistant_id": "asst-up"})["Item"]
        assert item["phone_number"] == "+12222222222"
        assert item["tenant_name"] == "New"


class TestListAssistants:
    """Tests for list_assistants."""

    def test_lists_all(self, _vapi_table, mock_settings):
        """Returns all registered assistants."""
        with patch("channels.vapi_config.get_settings", return_value=mock_settings):
            from channels.vapi_config import list_assistants, register_assistant

            register_assistant("a1", "+11111111111", "T1")
            register_assistant("a2", "+12222222222", "T2")
            result = list_assistants()

        assert len(result) == 2
        ids = {r["assistant_id"] for r in result}
        assert ids == {"a1", "a2"}

    def test_empty_table(self, _vapi_table, mock_settings):
        """Returns empty list when no assistants registered."""
        with patch("channels.vapi_config.get_settings", return_value=mock_settings):
            from channels.vapi_config import list_assistants

            result = list_assistants()

        assert result == []


class TestDeleteAssistant:
    """Tests for delete_assistant."""

    def test_deletes_item(self, _vapi_table, mock_settings):
        """Removes assistant config from DynamoDB."""
        with patch("channels.vapi_config.get_settings", return_value=mock_settings):
            from channels.vapi_config import _cache, delete_assistant, register_assistant

            _cache.clear()
            register_assistant("asst-del", "+13333333333", "Gone")
            result = delete_assistant("asst-del")

        assert result is True

        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        table = dynamodb.Table("pf-syn-schedulingagents-vapi-assistants-test")
        resp = table.get_item(Key={"assistant_id": "asst-del"})
        assert "Item" not in resp


class TestResolveToPhone:
    """Tests for _resolve_to_phone in vapi.py."""

    def test_uses_phone_number_dict(self):
        """Uses phoneNumber from call data when available (dict)."""
        from channels.vapi import _resolve_to_phone

        call_data = {"phoneNumber": {"number": "+14155551234"}}
        assert _resolve_to_phone(call_data) == "+14155551234"

    def test_uses_phone_number_str(self):
        """Uses phoneNumber from call data when available (string)."""
        from channels.vapi import _resolve_to_phone

        call_data = {"phoneNumber": "+14155551234"}
        assert _resolve_to_phone(call_data) == "+14155551234"

    def test_falls_back_to_assistant_config(self, _vapi_table, mock_settings):
        """Falls back to vapi_config lookup when phoneNumber is null."""
        with patch("channels.vapi_config.get_settings", return_value=mock_settings):
            from channels.vapi_config import _cache, register_assistant

            _cache.clear()
            register_assistant("asst-fb", "+19566699322", "Fallback")

        mock_settings.vapi_phone_number = ""
        with (
            patch("channels.vapi_config.get_settings", return_value=mock_settings),
            patch("channels.vapi.get_settings", return_value=mock_settings),
        ):
            from channels.vapi import _resolve_to_phone

            call_data = {"phoneNumber": None, "assistantId": "asst-fb"}
            result = _resolve_to_phone(call_data)

        assert result == "+19566699322"

    def test_falls_back_to_env_var(self):
        """Falls back to VAPI_PHONE_NUMBER env var as last resort."""
        settings = Settings(
            environment="test",
            aws_region="us-east-1",
            vapi_phone_number="+18005559999",
            vapi_assistants_table="",
        )
        with (
            patch("channels.vapi_config.get_settings", return_value=settings),
            patch("channels.vapi.get_settings", return_value=settings),
        ):
            from channels.vapi import _resolve_to_phone
            from channels.vapi_config import _cache

            _cache.clear()
            call_data = {"phoneNumber": None, "assistantId": "unknown"}
            result = _resolve_to_phone(call_data)

        assert result == "+18005559999"

    def test_returns_empty_when_all_fail(self):
        """Returns empty string when no phone source is available."""
        settings = Settings(
            environment="test",
            aws_region="us-east-1",
            vapi_phone_number="",
            vapi_assistants_table="",
        )
        with (
            patch("channels.vapi_config.get_settings", return_value=settings),
            patch("channels.vapi.get_settings", return_value=settings),
        ):
            from channels.vapi import _resolve_to_phone
            from channels.vapi_config import _cache

            _cache.clear()
            call_data = {"phoneNumber": None}
            result = _resolve_to_phone(call_data)

        assert result == ""
