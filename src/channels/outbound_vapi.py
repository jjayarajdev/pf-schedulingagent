"""Vapi API client for outbound calls."""

import logging

import httpx

from config import get_secrets

logger = logging.getLogger(__name__)

_VAPI_BASE_URL = "https://api.vapi.ai"
_TIMEOUT = httpx.Timeout(30.0)


def _log_curl(method: str, url: str, headers: dict, body: dict | None = None) -> None:
    """Log equivalent curl command for debugging."""
    safe = {k: v for k, v in headers.items() if k.lower() != "authorization"}
    logger.info("→ %s %s headers=%s body=%s", method, url, safe, body)


def _log_response(response: httpx.Response, label: str) -> None:
    """Log response status and body."""
    logger.info("← %s %d: %s", label, response.status_code, response.text[:500])


async def create_vapi_call(
    phone_number_id: str,
    customer_phone: str,
    customer_name: str,
    server_url: str,
    assistant_config: dict | None = None,
    server_block: dict | None = None,
    metadata: dict | None = None,
) -> dict:
    """Initiate an outbound call via Vapi POST /call.

    Vapi's POST /call requires either ``assistant`` (inline config) or
    ``assistantId``.  We pass the full assistant config inline — this
    includes a ``server`` block with our webhook URL so Vapi sends
    tool-calls back to us.

    The ``server_block`` is placed at the **top level** of the payload
    (not inside ``assistant``) so Vapi sends server events
    (end-of-call-report, status-update, conversation-update) to our
    webhook with the correct secret header.

    Args:
        phone_number_id: Vapi phone number ID to call FROM.
        customer_phone: Customer phone number to call (E.164).
        customer_name: Customer name (for Vapi metadata).
        server_url: Our webhook URL (set inside assistant.server.url).
        assistant_config: Full assistant config dict. If None, a minimal
            config is built pointing tool-calls to ``server_url``.
        server_block: Top-level server config for Vapi server events.
            Must contain ``url`` and ``secret`` keys.
        metadata: Pass-through metadata (includes our call_id).

    Returns:
        Vapi API response dict with call ID and status.

    Raises:
        httpx.HTTPStatusError: On non-2xx response from Vapi.
    """
    api_key = get_secrets().vapi_private_key
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Build assistant config if not provided
    if not assistant_config:
        server_secret = get_secrets().vapi_api_key
        assistant_config = {
            "server": {
                "url": server_url,
                "secret": server_secret,
            },
        }

    payload: dict = {
        "phoneNumberId": phone_number_id,
        "customer": {
            "number": customer_phone,
            "name": customer_name,
        },
        "assistant": assistant_config,
    }

    # NOTE: Vapi POST /call does NOT accept ``server`` or ``serverUrl`` at
    # the payload top level.  The server config (url + secret) lives inside
    # the ``assistant`` object and on each tool.  Vapi uses the assistant's
    # ``server`` block to send server events (end-of-call, status-update)
    # with the secret as the ``x-vapi-secret`` header.

    if metadata:
        payload["metadata"] = metadata

    url = f"{_VAPI_BASE_URL}/call"
    _log_curl("POST", url, headers, payload)

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(url, headers=headers, json=payload)

    _log_response(response, "create_vapi_call")
    response.raise_for_status()
    return response.json()


async def get_vapi_call_status(vapi_call_id: str) -> dict:
    """Check the status of a Vapi call.

    Args:
        vapi_call_id: Vapi's call ID.

    Returns:
        Vapi call status dict.
    """
    api_key = get_secrets().vapi_private_key
    headers = {"Authorization": f"Bearer {api_key}"}

    url = f"{_VAPI_BASE_URL}/call/{vapi_call_id}"
    _log_curl("GET", url, headers)

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(url, headers=headers)

    _log_response(response, "get_vapi_call_status")
    response.raise_for_status()
    return response.json()
