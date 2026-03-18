"""Phone-based authentication — DynamoDB-cached PF phone-call-login.

Async rewrite of v1.2.9's phone_auth.py.  Each phone number gets its own
DynamoDB row so multiple concurrent callers never overwrite each other.

Flow:
1. Normalize the incoming phone number.
2. Check DynamoDB for cached credentials (not expired / not within refresh buffer).
3. If valid cached creds exist, return them.
4. Otherwise call the PF ``/authentication/phone-call-login`` API, store the
   result in DynamoDB with a TTL, and return the fresh credentials.
"""

import logging
from datetime import UTC, datetime

import boto3
import httpx

from config import get_settings

logger = logging.getLogger(__name__)

# Refresh proactively if the token has fewer than this many seconds remaining.
TOKEN_REFRESH_BUFFER_SECONDS = 30


class AuthenticationError(Exception):
    """Raised when phone authentication fails."""


# ── Public API ────────────────────────────────────────────────────────────


async def get_or_authenticate(from_phone: str, to_phone: str = "") -> dict:
    """Return credentials for *from_phone*, authenticating via PF API if needed.

    Args:
        from_phone: Caller's phone number (e.g. ``+14702832382``).
        to_phone: System/destination phone number (optional — passed to the PF
            auth API when available).

    Returns:
        Dict with keys: ``bearer_token``, ``refresh_token``, ``client_id``,
        ``client_name``, ``user_id``, ``user_name``, ``user_phone``,
        ``user_email``, ``timezone``, ``exp``, ``customer_id``,
        ``support_number``, ``support_email``.

    Raises:
        AuthenticationError: If the phone number is missing or the PF API call fails.
    """
    if not from_phone:
        raise AuthenticationError("Missing caller phone number (from_phone)")

    phone = normalize_phone(from_phone)
    if not phone:
        raise AuthenticationError(f"Invalid caller phone number: {from_phone}")

    to_clean = normalize_phone(to_phone) if to_phone else ""

    # Step 1: Check DynamoDB cache
    cached = _get_cached_creds(phone)
    if cached:
        logger.info("Using cached credentials for ***%s", phone[-4:])
        return cached

    # Step 2: Call PF phone-call-login API
    logger.info("Authenticating ***%s via PF API", phone[-4:])
    try:
        credentials = await _call_auth_api(phone, to_clean)
    except AuthenticationError:
        if to_clean:
            logger.warning(
                "Auth failed with to_phone=***%s — retrying without it",
                to_clean[-4:],
            )
            credentials = await _call_auth_api(phone)
        else:
            raise

    # Step 3: Store in DynamoDB for subsequent requests
    _store_credentials(phone, credentials)
    logger.info(
        "Stored new credentials for user %s (phone: ***%s)",
        credentials.get("user_id", "?"),
        phone[-4:],
    )

    return credentials


def get_support_info(phone: str) -> dict:
    """Return support contact info from cached credentials for the given phone.

    Returns:
        Dict with ``support_number``, ``support_email``, and ``client_name``
        (empty strings if not available).
    """
    cached = _get_cached_creds(phone)
    if cached:
        return {
            "support_number": cached.get("support_number", ""),
            "support_email": cached.get("support_email", ""),
            "client_name": cached.get("client_name", "ProjectsForce"),
        }
    return {"support_number": "", "support_email": "", "client_name": "ProjectsForce"}


# ── Phone normalization ───────────────────────────────────────────────────


def normalize_phone(phone: str) -> str:
    """Strip to digits and remove country code prefix.

    Examples::

        +14702832382   -> 4702832382
        +918008455667  -> 8008455667
        1-470-283-2382 -> 4702832382
    """
    if not phone:
        return ""

    digits = "".join(ch for ch in phone if ch.isdigit())

    # US: 11 digits starting with 1 -> drop the leading 1
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]

    # India: 12 digits starting with 91 -> drop the leading 91
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]

    return digits


# ── DynamoDB cache ────────────────────────────────────────────────────────


def _get_cached_creds(phone: str) -> dict | None:
    """Return cached credentials from DynamoDB if they exist and are not expired."""
    settings = get_settings()
    try:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        table = dynamodb.Table(settings.phone_creds_table)
        response = table.get_item(Key={"phone_number": phone})

        if "Item" not in response:
            return None

        item = response["Item"]

        # Check token expiry
        exp = item.get("exp", 0)
        if hasattr(exp, "__float__"):
            exp = float(exp)

        now = datetime.now(UTC).timestamp()
        remaining = exp - now

        if remaining <= 0:
            logger.info("Token expired for ***%s", phone[-4:])
            return None

        if remaining < TOKEN_REFRESH_BUFFER_SECONDS:
            logger.info(
                "Token expiring in %.0fs for ***%s — refreshing proactively",
                remaining,
                phone[-4:],
            )
            return None

        logger.info("Token valid for ***%s — %.0fs remaining", phone[-4:], remaining)

        return {
            "bearer_token": item.get("bearer_token", ""),
            "refresh_token": item.get("refresh_token", ""),
            "client_id": item.get("client_id", ""),
            "client_name": item.get("client_name", "ProjectForce"),
            "user_id": item.get("user_id", ""),
            "user_name": item.get("user_name", ""),
            "user_phone": item.get("user_phone", ""),
            "user_email": item.get("user_email", ""),
            "customer_id": item.get("customer_id", ""),
            "timezone": item.get("timezone", "US/Eastern"),
            "exp": float(exp) if hasattr(exp, "__float__") else exp,
            "support_number": item.get("support_number", ""),
            "support_email": item.get("support_email", ""),
        }

    except Exception:
        logger.exception("Error reading credentials from DynamoDB for ***%s", phone[-4:])
        return None


def _store_credentials(phone: str, credentials: dict) -> None:
    """Persist credentials in DynamoDB keyed by phone number."""
    settings = get_settings()
    try:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        table = dynamodb.Table(settings.phone_creds_table)

        exp = credentials.get("exp", 0)
        # TTL = token expiration + 1 hour buffer for DynamoDB cleanup
        ttl = int(exp) + 3600 if exp else int(datetime.now(UTC).timestamp()) + 86400

        table.put_item(
            Item={
                "phone_number": phone,
                "bearer_token": credentials.get("bearer_token", ""),
                "refresh_token": credentials.get("refresh_token", ""),
                "client_id": credentials.get("client_id", ""),
                "client_name": credentials.get("client_name", "ProjectForce"),
                "user_id": credentials.get("user_id", ""),
                "user_name": credentials.get("user_name", ""),
                "user_phone": credentials.get("user_phone", phone),
                "user_email": credentials.get("user_email", ""),
                "customer_id": credentials.get("customer_id", ""),
                "timezone": credentials.get("timezone", "US/Eastern"),
                "exp": credentials.get("exp", 0),
                "updated_at": datetime.now(UTC).isoformat(),
                "ttl": ttl,
                "support_number": credentials.get("support_number", ""),
                "support_email": credentials.get("support_email", ""),
            }
        )
        logger.info("Stored credentials for ***%s (TTL: %d)", phone[-4:], ttl)

    except Exception:
        # Don't raise — credentials are still valid for this request,
        # they just won't be cached for the next one.
        logger.exception("Failed to store credentials in DynamoDB for ***%s", phone[-4:])


# ── PF Auth API call ──────────────────────────────────────────────────────


async def _call_auth_api(phone: str, to_phone: str = "") -> dict:
    """POST to PF ``/authentication/phone-call-login`` and return parsed credentials.

    Raises:
        AuthenticationError: On network errors, non-200 status, or missing access token.
    """
    settings = get_settings()
    url = f"{settings.pf_api_base_url}/authentication/phone-call-login"

    payload: dict = {"from_phone": phone}
    if to_phone:
        payload["to_phone"] = to_phone

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)

        if resp.status_code != 200:
            error_msg = resp.text[:200]
            logger.error("Phone auth API failed: %d — %s", resp.status_code, error_msg)
            raise AuthenticationError(f"Authentication failed: {error_msg}")

        data = resp.json()

        if "accesstoken" not in data:
            error_msg = data.get("message", "No access token in response")
            logger.error("Invalid phone auth response: %s", error_msg)
            raise AuthenticationError(f"Authentication failed: {error_msg}")

        user = data.get("user", {})

        return {
            "bearer_token": data["accesstoken"],
            "refresh_token": data.get("refrestoken", ""),  # Note: PF API typo
            "client_id": data.get("client_id", user.get("client_id", "")),
            "client_name": data.get("client_name", "ProjectForce"),
            "user_id": str(user.get("customer_id", "")),
            "user_name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
            "user_phone": phone,
            "user_email": user.get("email", ""),
            "customer_id": str(user.get("customer_id", "")),
            "timezone": data.get("timezone", "US/Eastern"),
            "exp": data.get("exp", 0),
            "updated_at": datetime.now(UTC).isoformat(),
            "support_number": data.get("support_number", ""),
            "support_email": data.get("support_email_1", ""),
        }

    except httpx.HTTPError as exc:
        logger.exception("Phone auth API request error")
        raise AuthenticationError(f"Authentication request failed: {exc}") from exc
