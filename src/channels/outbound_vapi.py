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
    metadata: dict | None = None,
) -> dict:
    """Initiate an outbound call via Vapi POST /call.

    Uses serverUrl mode — Vapi will send assistant-request back to our webhook,
    where we return the outbound-specific assistant config dynamically.

    Args:
        phone_number_id: Vapi phone number ID to call FROM
        customer_phone: Customer phone number to call (E.164)
        customer_name: Customer name (for Vapi metadata)
        server_url: Our webhook URL (Vapi sends assistant-request here)
        metadata: Pass-through metadata (includes our call_id)

    Returns:
        Vapi API response dict with call ID and status.

    Raises:
        httpx.HTTPStatusError: On non-2xx response from Vapi.
    """
    api_key = get_secrets().vapi_api_key
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "phoneNumberId": phone_number_id,
        "customer": {
            "number": customer_phone,
            "name": customer_name,
        },
        "serverUrl": server_url,
    }
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
    api_key = get_secrets().vapi_api_key
    headers = {"Authorization": f"Bearer {api_key}"}

    url = f"{_VAPI_BASE_URL}/call/{vapi_call_id}"
    _log_curl("GET", url, headers)

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(url, headers=headers)

    _log_response(response, "get_vapi_call_status")
    response.raise_for_status()
    return response.json()
