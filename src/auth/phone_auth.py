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

# Maximum cache duration (seconds). Re-authenticate after this even if token is
# still valid, so stale phone→customer mappings don't persist too long.
MAX_CACHE_SECONDS = 900  # 15 minutes


class AuthenticationError(Exception):
    """Raised when phone authentication fails."""

    def __init__(
        self,
        message: str,
        status_code: int = 0,
        client_id: str = "",
        client_name: str = "",
        support_number: str = "",
        office_hours: list | None = None,
        timezone: str = "",
    ):
        super().__init__(message)
        self.status_code = status_code
        self.client_id = client_id
        self.client_name = client_name
        self.support_number = support_number
        self.office_hours = office_hours or []
        self.timezone = timezone


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

    # Cache key includes to_phone so the same caller dialing different tenants
    # gets separate cached credentials (e.g. WTU vs ProjectsForce).
    cache_key = f"{phone}:{to_clean}" if to_clean else phone

    # Step 1: Check DynamoDB cache
    cached = _get_cached_creds(cache_key)
    if cached:
        logger.info("Using cached credentials for ***%s (to=***%s)", phone[-4:], to_clean[-4:] if to_clean else "none")
        return cached

    # Step 2: Call PF phone-call-login API
    logger.info(
        "Authenticating ***%s via PF API (to_phone=%s)",
        phone[-4:],
        f"***{to_clean[-4:]}" if to_clean else "none",
    )
    try:
        credentials = await _call_auth_api(phone, to_clean)
    except AuthenticationError as exc:
        # status_code == 0 means 200 with no token (valid "not a customer")
        # — no point retrying. Only retry on actual API failures (non-200).
        if to_clean and exc.status_code != 0:
            logger.warning(
                "Auth failed with to_phone=***%s — retrying without it",
                to_clean[-4:],
            )
            try:
                credentials = await _call_auth_api(phone)
            except AuthenticationError as retry_exc:
                _enrich_error_from_cache(retry_exc, phone)
                raise
        else:
            _enrich_error_from_cache(exc, phone)
            raise

    # Step 3: Store in DynamoDB for subsequent requests
    _store_credentials(cache_key, credentials)
    save_tenant_config(credentials)
    logger.info(
        "Stored new credentials for user %s (phone: ***%s)",
        credentials.get("user_id", "?"),
        phone[-4:],
    )

    return credentials


def _enrich_error_from_cache(exc: "AuthenticationError", phone: str) -> None:
    """Populate an AuthenticationError with support info from expired cache or tenant config."""
    # Try expired phone cache first (has all fields)
    info = _get_cached_support_info(phone)
    if not info and exc.client_id:
        # Fall back to tenant config (keyed by client_id, has office_hours + timezone)
        info = get_tenant_config(exc.client_id)
    if not info:
        return
    if not exc.client_name:
        exc.client_name = info.get("client_name", "")
    if not exc.support_number:
        exc.support_number = info.get("support_number", "")
    if not exc.office_hours:
        exc.office_hours = info.get("office_hours", [])
    if not exc.timezone:
        exc.timezone = info.get("timezone", "")


def _get_cached_support_info(phone: str) -> dict:
    """Read support info from DynamoDB ignoring token expiry.

    Used to enrich AuthenticationError when a known caller's token has expired
    and re-auth fails — we still want the support_number, office_hours, etc.
    """
    settings = get_settings()
    try:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        table = dynamodb.Table(settings.phone_creds_table)
        response = table.get_item(Key={"phone_number": phone})
        if "Item" not in response:
            return {}
        item = response["Item"]
        return {
            "support_number": item.get("support_number", ""),
            "client_name": item.get("client_name", ""),
            "office_hours": item.get("office_hours", []),
            "timezone": item.get("timezone", ""),
        }
    except Exception:
        logger.exception("Error reading support info from DynamoDB for ***%s", phone[-4:])
        return {}


def get_tenant_config(client_id: str) -> dict:
    """Read tenant-level config (office_hours, timezone) from DynamoDB.

    Keyed by ``client:{client_id}`` in the phone-creds table.
    """
    if not client_id:
        return {}
    settings = get_settings()
    try:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        table = dynamodb.Table(settings.phone_creds_table)
        response = table.get_item(Key={"phone_number": f"client:{client_id}"})
        if "Item" not in response:
            return {}
        item = response["Item"]
        return {
            "office_hours": item.get("office_hours", []),
            "timezone": item.get("timezone", ""),
            "support_number": item.get("support_number", ""),
            "client_name": item.get("client_name", ""),
        }
    except Exception:
        logger.exception("Error reading tenant config for client %s", client_id)
        return {}


def save_tenant_config(credentials: dict) -> None:
    """Persist tenant-level config (office_hours, timezone) keyed by client_id.

    Called on any successful auth that returns office_hours.
    """
    client_id = credentials.get("client_id", "")
    office_hours = credentials.get("office_hours", [])
    if not client_id or not office_hours:
        return
    settings = get_settings()
    try:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        table = dynamodb.Table(settings.phone_creds_table)
        table.put_item(Item={
            "phone_number": f"client:{client_id}",
            "client_id": client_id,
            "client_name": credentials.get("client_name", ""),
            "support_number": credentials.get("support_number", ""),
            "timezone": credentials.get("timezone", "US/Eastern"),
            "office_hours": office_hours,
            "updated_at": datetime.now(UTC).isoformat(),
        })
        logger.info("Saved tenant config for client %s", client_id)
    except Exception:
        logger.exception("Failed to save tenant config for client %s", client_id)


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


def get_cached_auth(phone: str) -> dict | None:
    """Return cached auth credentials for end-of-call note posting.

    Returns dict with ``bearer_token``, ``client_id``, ``customer_id``
    or None if not cached / expired.
    """
    cached = _get_cached_creds(phone)
    if not cached:
        return None
    return {
        "bearer_token": cached.get("bearer_token", ""),
        "client_id": cached.get("client_id", ""),
        "customer_id": cached.get("customer_id", cached.get("user_id", "")),
    }


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

        # Enforce max cache age — re-authenticate if stored too long ago
        updated_at = item.get("updated_at", "")
        if updated_at:
            try:
                stored_ts = datetime.fromisoformat(updated_at).timestamp()
                cache_age = now - stored_ts
                if cache_age > MAX_CACHE_SECONDS:
                    logger.info(
                        "Cache expired for ***%s — age %.0fs > %ds",
                        phone[-4:], cache_age, MAX_CACHE_SECONDS,
                    )
                    return None
            except (ValueError, TypeError):
                pass  # If updated_at is malformed, fall through to token-based check

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
            "office_hours": item.get("office_hours", []),
        }

    except Exception:
        logger.exception("Error reading credentials from DynamoDB for ***%s", phone[-4:])
        return None


def delete_cached_creds(phone: str) -> bool:
    """Delete cached credentials for a phone number. Returns True if deleted."""
    normalized = normalize_phone(phone) if phone else ""
    if not normalized:
        return False

    settings = get_settings()
    try:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        table = dynamodb.Table(settings.phone_creds_table)
        table.delete_item(Key={"phone_number": normalized})
        logger.info("Deleted cached credentials for ***%s", normalized[-4:])
        return True
    except Exception:
        logger.exception("Failed to delete cached credentials for ***%s", normalized[-4:])
        return False


def _store_credentials(phone: str, credentials: dict) -> None:
    """Persist credentials in DynamoDB keyed by phone number."""
    settings = get_settings()
    try:
        dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
        table = dynamodb.Table(settings.phone_creds_table)

        exp = credentials.get("exp", 0)
        # TTL = cache max age + 5 min buffer for DynamoDB cleanup
        ttl = int(datetime.now(UTC).timestamp()) + MAX_CACHE_SECONDS + 300

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
                "office_hours": credentials.get("office_hours", []),
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
            raise AuthenticationError(
                f"Authentication failed: {error_msg}",
                status_code=resp.status_code,
            )

        data = resp.json()

        if "accesstoken" not in data:
            error_msg = data.get("message", "No access token in response")
            logger.info("Phone auth: no token (non-customer) for ***%s", phone[-4:])
            raise AuthenticationError(
                f"Authentication failed: {error_msg}",
                client_id=data.get("client_id", ""),
                client_name=data.get("client_name", ""),
                support_number=data.get("support_number", ""),
                office_hours=data.get("office_hours", []),
                timezone=data.get("timezone", ""),
            )

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
            "office_hours": data.get("office_hours", []),
        }

    except httpx.HTTPError as exc:
        logger.exception("Phone auth API request error")
        raise AuthenticationError(f"Authentication request failed: {exc}") from exc


# ── Store authentication ─────────────────────────────────────────────────


async def authenticate_store(
    tenant_phone: str, lookup_type: str, lookup_value: str
) -> dict:
    """Authenticate a store caller via POST /authentication/store-login.

    Args:
        tenant_phone: The destination phone number (identifies the tenant).
        lookup_type: One of ``project_number``, ``po_number``, ``customer_name``.
        lookup_value: The value to look up (e.g. a PO number or customer name).

    Returns:
        Credentials dict with the same keys as ``get_or_authenticate``.

    Raises:
        AuthenticationError: On network errors, non-200 status, or missing token.
    """
    if not lookup_type or not lookup_value:
        raise AuthenticationError("Missing lookup_type or lookup_value for store login")

    # Check DynamoDB cache first
    cache_key = f"store:{tenant_phone}:{lookup_value}"
    cached = _get_cached_creds(cache_key)
    if cached:
        logger.info("Using cached store credentials for %s", lookup_value)
        return cached

    settings = get_settings()
    url = f"{settings.pf_api_base_url}/authentication/store-login"
    payload = {
        "tenant_phone": tenant_phone,
        "lookup_type": lookup_type,
        "lookup_value": lookup_value,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)

        if resp.status_code != 200:
            error_msg = resp.text[:200]
            logger.error("Store login API failed: %d — %s", resp.status_code, error_msg)
            raise AuthenticationError(
                f"Store authentication failed: {error_msg}",
                status_code=resp.status_code,
            )

        data = resp.json()

        if "accesstoken" not in data:
            error_msg = data.get("message", "No access token in response")
            logger.error("Invalid store login response: %s", error_msg)
            raise AuthenticationError(f"Store authentication failed: {error_msg}")

        user = data.get("user", {})

        credentials = {
            "bearer_token": data["accesstoken"],
            "refresh_token": data.get("refrestoken", ""),
            "client_id": data.get("client_id", user.get("client_id", "")),
            "client_name": data.get("client_name", "ProjectForce"),
            "user_id": str(user.get("customer_id", "")),
            "user_name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
            "user_phone": "",
            "user_email": "",
            "customer_id": str(user.get("customer_id", "")),
            "timezone": data.get("timezone", "US/Eastern"),
            "exp": data.get("exp", 0),
            "updated_at": datetime.now(UTC).isoformat(),
            "support_number": data.get("support_number", ""),
            "support_email": data.get("support_email_1", ""),
            "office_hours": data.get("office_hours", []),
        }

        _store_credentials(cache_key, credentials)
        save_tenant_config(credentials)
        logger.info(
            "Store login success: tenant=***%s lookup=%s:%s user=%s",
            tenant_phone[-4:] if tenant_phone else "none",
            lookup_type,
            lookup_value,
            credentials.get("user_id", "?"),
        )
        return credentials

    except httpx.HTTPError as exc:
        logger.exception("Store login API request error")
        raise AuthenticationError(f"Store authentication request failed: {exc}") from exc
