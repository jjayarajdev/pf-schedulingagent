"""Tests for outbound_store.py — DynamoDB CRUD and in-memory cache."""

from unittest.mock import MagicMock, patch

import pytest

from channels.outbound_store import (
    _active_calls,
    cache_active_call,
    create_outbound_call,
    get_active_call,
    get_calls_for_project,
    get_outbound_call,
    remove_active_call,
    update_outbound_call,
)


@pytest.fixture(autouse=True)
def _clear_active_calls():
    _active_calls.clear()
    yield
    _active_calls.clear()


# ── In-memory cache tests ─────────────────────────────────────────


class TestActiveCallCache:
    def test_cache_and_retrieve(self):
        data = {"call_id": "c1", "project_id": "p1"}
        cache_active_call("vapi-123", data)
        assert get_active_call("vapi-123") == data

    def test_get_missing_returns_none(self):
        assert get_active_call("nonexistent") is None

    def test_remove_clears_entry(self):
        cache_active_call("vapi-456", {"call_id": "c2"})
        remove_active_call("vapi-456")
        assert get_active_call("vapi-456") is None

    def test_remove_missing_does_not_raise(self):
        remove_active_call("never-existed")

    def test_multiple_entries(self):
        cache_active_call("v1", {"call_id": "c1"})
        cache_active_call("v2", {"call_id": "c2"})
        assert get_active_call("v1")["call_id"] == "c1"
        assert get_active_call("v2")["call_id"] == "c2"
        remove_active_call("v1")
        assert get_active_call("v1") is None
        assert get_active_call("v2") is not None


# ── DynamoDB CRUD tests ───────────────────────────────────────────


@pytest.fixture()
def mock_table():
    table = MagicMock()
    with patch("channels.outbound_store._get_table", return_value=table):
        yield table


class TestCreateOutboundCall:
    @pytest.mark.asyncio
    async def test_creates_with_uuid(self, mock_table):
        call_id = await create_outbound_call({
            "project_id": "proj-1",
            "customer_phone": "+15551234567",
        })

        assert call_id  # UUID generated
        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["call_id"] == call_id
        assert item["project_id"] == "proj-1"
        assert item["status"] == "pending"
        assert item["attempt_number"] == 1
        assert "created_at" in item
        assert "ttl" in item

    @pytest.mark.asyncio
    async def test_preserves_provided_call_id(self, mock_table):
        call_id = await create_outbound_call({
            "call_id": "my-id",
            "project_id": "proj-2",
        })

        assert call_id == "my-id"
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["call_id"] == "my-id"

    @pytest.mark.asyncio
    async def test_raises_on_dynamo_error(self, mock_table):
        mock_table.put_item.side_effect = Exception("DDB error")
        with pytest.raises(Exception, match="DDB error"):
            await create_outbound_call({"project_id": "p"})


class TestGetOutboundCall:
    @pytest.mark.asyncio
    async def test_returns_item(self, mock_table):
        mock_table.get_item.return_value = {"Item": {"call_id": "c1", "status": "calling"}}
        result = await get_outbound_call("c1")
        assert result["call_id"] == "c1"
        assert result["status"] == "calling"

    @pytest.mark.asyncio
    async def test_returns_none_when_missing(self, mock_table):
        mock_table.get_item.return_value = {}
        result = await get_outbound_call("c2")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_error(self, mock_table):
        mock_table.get_item.side_effect = Exception("DDB error")
        result = await get_outbound_call("c3")
        assert result is None


class TestUpdateOutboundCall:
    @pytest.mark.asyncio
    async def test_updates_fields(self, mock_table):
        await update_outbound_call("c1", {"status": "completed", "vapi_call_id": "v1"})

        mock_table.update_item.assert_called_once()
        kwargs = mock_table.update_item.call_args[1]
        assert kwargs["Key"] == {"call_id": "c1"}
        assert "SET" in kwargs["UpdateExpression"]
        # Should include updated_at automatically
        all_names = kwargs["ExpressionAttributeNames"]
        has_updated_key = any(v == "updated_at" for v in all_names.values())
        assert has_updated_key

    @pytest.mark.asyncio
    async def test_raises_on_error(self, mock_table):
        mock_table.update_item.side_effect = Exception("DDB error")
        with pytest.raises(Exception, match="DDB error"):
            await update_outbound_call("c1", {"status": "failed"})


class TestGetCallsForProject:
    @pytest.mark.asyncio
    async def test_queries_gsi(self, mock_table):
        mock_table.query.return_value = {
            "Items": [
                {"call_id": "c1", "status": "completed"},
                {"call_id": "c2", "status": "calling"},
            ]
        }

        result = await get_calls_for_project("proj-1")

        assert len(result) == 2
        mock_table.query.assert_called_once()
        kwargs = mock_table.query.call_args[1]
        assert kwargs["IndexName"] == "project-calls-index"
        assert kwargs["ScanIndexForward"] is False

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self, mock_table):
        mock_table.query.side_effect = Exception("DDB error")
        result = await get_calls_for_project("proj-bad")
        assert result == []
