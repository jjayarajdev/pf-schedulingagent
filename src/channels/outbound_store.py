"""DynamoDB store and in-memory cache for outbound call records."""

import logging
import time
import uuid
from datetime import datetime, timezone

import boto3

from config import get_settings

logger = logging.getLogger(__name__)

# Table reference (lazy init)
_table = None
_TABLE_TTL_DAYS = 30


def _get_table():
    global _table
    if _table is None:
        settings = get_settings()
        resource = boto3.resource("dynamodb", region_name=settings.aws_region)
        _table = resource.Table(settings.outbound_calls_table)
    return _table


# ── In-memory cache for active calls ────────────────────────────────
# Keyed by vapi_call_id → call_data dict
# Used to avoid DynamoDB reads on every tool-call event during an active call
_active_calls: dict[str, dict] = {}


def cache_active_call(vapi_call_id: str, call_data: dict) -> None:
    _active_calls[vapi_call_id] = call_data


def get_active_call(vapi_call_id: str) -> dict | None:
    return _active_calls.get(vapi_call_id)


def remove_active_call(vapi_call_id: str) -> None:
    _active_calls.pop(vapi_call_id, None)


# ── DynamoDB CRUD ───────────────────────────────────────────────────


async def create_outbound_call(call_data: dict) -> str:
    """Create an outbound call record. Returns the call_id."""
    call_id = call_data.get("call_id") or str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    ttl = int(time.time()) + (_TABLE_TTL_DAYS * 86400)

    item = {
        "call_id": call_id,
        "status": "pending",
        "attempt_number": 1,
        "max_attempts": 2,
        "created_at": now,
        "updated_at": now,
        "ttl": ttl,
        **call_data,
        "call_id": call_id,  # ensure not overwritten
    }

    try:
        _get_table().put_item(Item=item)
        logger.info("Created outbound call record: %s", call_id)
    except Exception:
        logger.exception("Failed to create outbound call: %s", call_id)
        raise

    return call_id


async def get_outbound_call(call_id: str) -> dict | None:
    """Get an outbound call record by call_id."""
    try:
        resp = _get_table().get_item(Key={"call_id": call_id})
        return resp.get("Item")
    except Exception:
        logger.exception("Failed to get outbound call: %s", call_id)
        return None


async def update_outbound_call(call_id: str, updates: dict) -> None:
    """Update fields on an outbound call record."""
    now = datetime.now(timezone.utc).isoformat()
    updates["updated_at"] = now

    expr_parts = []
    expr_values = {}
    expr_names = {}
    for i, (key, val) in enumerate(updates.items()):
        alias = f"#k{i}"
        placeholder = f":v{i}"
        expr_parts.append(f"{alias} = {placeholder}")
        expr_names[alias] = key
        expr_values[placeholder] = val

    try:
        _get_table().update_item(
            Key={"call_id": call_id},
            UpdateExpression="SET " + ", ".join(expr_parts),
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
        logger.info("Updated outbound call %s: %s", call_id, list(updates.keys()))
    except Exception:
        logger.exception("Failed to update outbound call: %s", call_id)
        raise


async def get_calls_for_project(project_id: str) -> list[dict]:
    """Get all outbound calls for a project (via GSI)."""
    try:
        resp = _get_table().query(
            IndexName="project-calls-index",
            KeyConditionExpression="project_id = :pid",
            ExpressionAttributeValues={":pid": project_id},
            ScanIndexForward=False,  # newest first
        )
        return resp.get("Items", [])
    except Exception:
        logger.exception("Failed to query calls for project: %s", project_id)
        return []
