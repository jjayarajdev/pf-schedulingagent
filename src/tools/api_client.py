"""Shared PF API helpers -- headers, URL building, debug logging.

Mirrors the pattern from ai-support's ``role_permissions.py`` but adapted
for the scheduling bot's context (AuthContext carries auth_token, client_id,
and customer_id).
"""

import json
import logging

import httpx

from auth.context import AuthContext
from config import get_settings

logger = logging.getLogger(__name__)


def build_headers() -> dict[str, str]:
    """Build common PF API headers from AuthContext.

    Matches the v1.2.9 header pattern: Authorization, Accept, Content-Type, client_id.
    """
    return {
        "Authorization": f"Bearer {AuthContext.get_auth_token()}",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "client_id": AuthContext.get_client_id(),
    }


def get_pf_api_base() -> str:
    """Return the PF API base URL from settings."""
    return get_settings().pf_api_base_url


def log_curl(method: str, url: str, headers: dict[str, str], body: dict | None = None) -> None:
    """Log the equivalent ``curl`` command for a PF API call.

    The Authorization token is masked to avoid leaking secrets into logs.
    """
    token = headers.get("Authorization", "")
    masked = f"{token[:20]}...{token[-10:]}" if len(token) > 40 else "***"

    header_parts: list[str] = []
    for key, value in headers.items():
        display = masked if key == "Authorization" else value
        header_parts.append(f"-H '{key}: {display}'")

    headers_str = " \\\n  ".join(header_parts)
    data_str = f" \\\n  --data-raw '{json.dumps(body)}'" if body else ""

    logger.info(
        "PF API curl:\ncurl -X %s '%s' \\\n  %s%s",
        method,
        url,
        headers_str,
        data_str,
    )


def log_response(response: httpx.Response, label: str = "") -> None:
    """Log the PF API response (status + truncated body)."""
    prefix = f"{label} " if label else ""
    logger.info(
        "%sPF API response: %s %s -> %d\n%s",
        prefix,
        response.request.method,
        str(response.url),
        response.status_code,
        response.text[:2000],
    )
