"""Vapi assistant configuration — DynamoDB-backed with in-memory cache.

Maps Vapi assistant IDs to their phone numbers and tenant metadata.
Since all Vapi numbers are Vapi-managed, the ``phoneNumber`` field in
webhook call data is always null.  This module resolves the correct
phone number for each assistant so the PF phone-call-login API receives
a valid ``to_phone``.

Usage::

    phone = get_phone_for_assistant("c53cebb0-...")  # cached lookup
    register_assistant("c53cebb0-...", "+19566699322", "Acme Corp")
"""

import logging
import time
from datetime import UTC, datetime

import boto3

from config import get_settings

logger = logging.getLogger(__name__)

# In-memory cache: assistant_id -> (phone_number, timestamp)
_cache: dict[str, tuple[str, float]] = {}
_CACHE_TTL_SECONDS = 300  # 5 minutes


def get_phone_for_assistant(assistant_id: str) -> str:
    """Return the Vapi phone number for the given assistant ID.

    Checks in-memory cache first, then DynamoDB.  Returns empty string
    if the assistant is not registered.
    """
    if not assistant_id:
        return ""

    # Check in-memory cache
    cached = _cache.get(assistant_id)
    if cached:
        phone, ts = cached
        if time.monotonic() - ts < _CACHE_TTL_SECONDS:
            return phone
        # Expired — fall through to DynamoDB
        del _cache[assistant_id]

    # DynamoDB lookup
    settings = get_settings()
    table_name = settings.vapi_assistants_table
    if not table_name:
        return ""

    try:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        table = dynamodb.Table(table_name)
        response = table.get_item(Key={"assistant_id": assistant_id})

        if "Item" not in response:
            logger.warning("No Vapi config for assistant %s", assistant_id)
            return ""

        phone = response["Item"].get("phone_number", "")
        if phone:
            _cache[assistant_id] = (phone, time.monotonic())
            logger.info(
                "Resolved Vapi phone for assistant %s: ***%s",
                assistant_id[:8],
                phone[-4:],
            )
        return phone

    except Exception:
        logger.exception("Failed to look up Vapi assistant config: %s", assistant_id)
        return ""


def register_assistant(
    assistant_id: str,
    phone_number: str,
    tenant_name: str = "",
) -> None:
    """Register or update a Vapi assistant → phone number mapping."""
    settings = get_settings()
    table_name = settings.vapi_assistants_table
    if not table_name:
        logger.error("vapi_assistants_table not configured — cannot register assistant")
        return

    dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
    table = dynamodb.Table(table_name)

    now = datetime.now(UTC).isoformat()
    table.put_item(
        Item={
            "assistant_id": assistant_id,
            "phone_number": phone_number,
            "tenant_name": tenant_name,
            "updated_at": now,
            "created_at": now,
        }
    )

    # Update cache
    _cache[assistant_id] = (phone_number, time.monotonic())
    logger.info(
        "Registered Vapi assistant %s → %s (tenant: %s)",
        assistant_id[:8],
        phone_number,
        tenant_name or "—",
    )


def list_assistants() -> list[dict]:
    """Return all registered Vapi assistant configs."""
    settings = get_settings()
    table_name = settings.vapi_assistants_table
    if not table_name:
        return []

    try:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        table = dynamodb.Table(table_name)
        response = table.scan()
        return response.get("Items", [])
    except Exception:
        logger.exception("Failed to list Vapi assistant configs")
        return []


def delete_assistant(assistant_id: str) -> bool:
    """Remove a Vapi assistant config. Returns True if deleted."""
    settings = get_settings()
    table_name = settings.vapi_assistants_table
    if not table_name:
        return False

    try:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        table = dynamodb.Table(table_name)
        table.delete_item(Key={"assistant_id": assistant_id})
        _cache.pop(assistant_id, None)
        logger.info("Deleted Vapi assistant config: %s", assistant_id)
        return True
    except Exception:
        logger.exception("Failed to delete Vapi assistant config: %s", assistant_id)
        return False
