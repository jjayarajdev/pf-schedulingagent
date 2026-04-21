"""Tests for store caller flow — auth, PII filter, AuthContext extensions."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth.context import AuthContext
from auth.phone_auth import AuthenticationError, authenticate_store
from tools.pii_filter import scrub_pii


# ── AuthContext caller_type / tenant_phone ──────────────────────────────


class TestAuthContextExtensions:
    """New caller_type and tenant_phone contextvars."""

    def test_default_caller_type(self):
        assert AuthContext.get_caller_type() == "customer"

    def test_default_tenant_phone(self):
        assert AuthContext.get_tenant_phone() == ""

    def test_set_caller_type(self):
        AuthContext.set(caller_type="store")
        assert AuthContext.get_caller_type() == "store"

    def test_set_tenant_phone(self):
        AuthContext.set(tenant_phone="4702832382")
        assert AuthContext.get_tenant_phone() == "4702832382"

    def test_clear_resets_caller_type(self):
        AuthContext.set(caller_type="store", tenant_phone="1234567890")
        AuthContext.clear()
        assert AuthContext.get_caller_type() == "customer"
        assert AuthContext.get_tenant_phone() == ""

    def test_set_preserves_other_fields(self):
        AuthContext.set(auth_token="tok", client_id="cid", caller_type="store")
        assert AuthContext.get_auth_token() == "tok"
        assert AuthContext.get_client_id() == "cid"
        assert AuthContext.get_caller_type() == "store"


# ── AuthenticationError status_code ─────────────────────────────────────


class TestAuthenticationError:
    """AuthenticationError now carries status_code."""

    def test_default_status_code(self):
        err = AuthenticationError("fail")
        assert err.status_code == 0
        assert str(err) == "fail"

    def test_custom_status_code(self):
        err = AuthenticationError("not found", status_code=404)
        assert err.status_code == 404

    def test_server_error_code(self):
        err = AuthenticationError("server error", status_code=500)
        assert err.status_code == 500


# ── authenticate_store() ───────────────────────────────────────────────


class TestAuthenticateStore:
    """POST /authentication/store-login integration."""

    @pytest.mark.asyncio
    async def test_missing_lookup_raises(self):
        with pytest.raises(AuthenticationError, match="Missing lookup_type"):
            await authenticate_store("4702832382", "", "")

    @pytest.mark.asyncio
    async def test_missing_lookup_value_raises(self):
        with pytest.raises(AuthenticationError, match="Missing lookup_type"):
            await authenticate_store("4702832382", "po_number", "")

    @pytest.mark.asyncio
    @patch("auth.phone_auth._get_cached_creds", return_value=None)
    @patch("auth.phone_auth._store_credentials")
    async def test_success(self, mock_store, mock_cache):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "auth_status": "success",
            "caller_type": "store",
            "accesstoken": "store-token-abc",
            "refrestoken": "refresh-xyz",
            "exp": 1774146061,
            "user": {
                "customer_id": 1645869,
                "client_id": "09PF05VD",
                "first_name": "rk9",
                "last_name": "rk9",
            },
            "client_id": "09PF05VD",
            "timezone": "US/Eastern",
            "client_name": "ProjectsForce Validation",
            "support_number": "(767) 676-7678",
            "support_email_1": "support@test.com",
        }

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            result = await authenticate_store("4702832382", "po_number", "PO-123")

        assert result["bearer_token"] == "store-token-abc"
        assert result["client_id"] == "09PF05VD"
        assert result["customer_id"] == "1645869"
        assert result["user_name"] == "rk9 rk9"
        assert result["timezone"] == "US/Eastern"
        mock_store.assert_called_once()

    @pytest.mark.asyncio
    @patch("auth.phone_auth._get_cached_creds", return_value=None)
    async def test_non_200_raises(self, mock_cache):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "Not found"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_ctx):
            with pytest.raises(AuthenticationError) as exc_info:
                await authenticate_store("4702832382", "po_number", "BAD-PO")
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    @patch("auth.phone_auth._get_cached_creds")
    async def test_cached_creds_returned(self, mock_cache):
        cached = {
            "bearer_token": "cached-token",
            "client_id": "CACHED",
            "customer_id": "999",
        }
        mock_cache.return_value = cached

        result = await authenticate_store("4702832382", "po_number", "PO-123")
        assert result["bearer_token"] == "cached-token"


# ── PII scrubber ────────────────────────────────────────────────────────


class TestPiiScrubber:
    """scrub_pii() strips phone numbers, emails, and street addresses."""

    def test_empty_string(self):
        assert scrub_pii("") == ""

    def test_no_pii(self):
        text = "Your project is scheduled for March 20, 2026."
        assert scrub_pii(text) == text

    def test_phone_parens(self):
        result = scrub_pii("Call (470) 283-2382 for info.")
        assert "470" not in result
        assert "[redacted]" in result

    def test_phone_dashes(self):
        result = scrub_pii("Number: 470-283-2382")
        assert "470" not in result

    def test_phone_plus1(self):
        result = scrub_pii("Reach me at +14702832382")
        assert "4702832382" not in result

    def test_email(self):
        result = scrub_pii("Email john.doe@example.com for details.")
        assert "john.doe@example.com" not in result
        assert "[redacted]" in result

    def test_street_address(self):
        result = scrub_pii("Located at 123 Main Street, Atlanta.")
        assert "123 Main Street" not in result
        assert "[redacted]" in result

    def test_street_abbreviation(self):
        result = scrub_pii("Address: 4500 Oak Blvd")
        assert "4500 Oak Blvd" not in result

    def test_multiple_pii(self):
        text = "Phone: (555) 123-4567, email: test@x.com, at 100 Elm Dr."
        result = scrub_pii(text)
        assert "555" not in result
        assert "test@x.com" not in result
        assert "100 Elm" not in result

    def test_preserves_project_numbers(self):
        """Project IDs and dates should NOT be redacted."""
        text = "Project 90000149 is scheduled for 2026-03-20."
        result = scrub_pii(text)
        assert "90000149" in result
        assert "2026-03-20" in result


# ── scheduling.py address1 exclusion ────────────────────────────────────


class TestProjectMinimalStoreFilter:
    """_extract_project_minimal limits fields for store callers."""

    _FULL_ITEM = {
        "project_project_id": "123",
        "project_project_number": "PO-1",
        "status_info_status": "Scheduled",
        "project_category_category": "Windows",
        "project_type_project_type": "Installation",
        "convertedProjectStartScheduledDate": "2026-03-25",
        "convertedProjectEndScheduledDate": "2026-03-25",
        "user_idata_first_name": "John",
        "user_idata_last_name": "Doe",
        "installer_details_installer_id": "inst-99",
        "installation_address_address1": "456 Oak St",
        "installation_address_city": "Atlanta",
        "installation_address_state": "GA",
        "installation_address_zipcode": "30301",
        "store_info_store_name": "Home Depot #1234",
        "store_info_store_number": "1234",
    }

    def test_customer_gets_full_data(self):
        from tools.scheduling import _extract_project_minimal

        AuthContext.set(caller_type="customer")
        result = _extract_project_minimal(self._FULL_ITEM)
        assert result["address"]["address1"] == "456 Oak St"
        assert result["category"] == "Windows"
        assert result["projectType"] == "Installation"
        assert result["store"]["storeName"] == "Home Depot #1234"
        assert result["installer"]["id"] == "inst-99"

    def test_store_only_gets_allowed_fields(self):
        from tools.scheduling import _extract_project_minimal

        AuthContext.set(caller_type="store")
        result = _extract_project_minimal(self._FULL_ITEM)
        # Allowed: status, scheduledDate, scheduledEndDate, installer name
        assert result["status"] == "Scheduled"
        assert result["scheduledDate"] == "2026-03-25"
        assert result["installer"]["name"] == "John Doe"
        # projectType IS allowed (agent needs it to identify projects)
        assert "projectType" in result
        # NOT allowed: address, category, store, installer id
        assert "address" not in result
        assert "category" not in result
        assert "store" not in result
        assert "id" not in result.get("installer", {})

    def test_store_without_schedule_or_installer(self):
        from tools.scheduling import _extract_project_minimal

        item = {
            "project_project_id": "456",
            "project_project_number": "PO-2",
            "status_info_status": "New",
        }
        AuthContext.set(caller_type="store")
        result = _extract_project_minimal(item)
        assert result["status"] == "New"
        assert "scheduledDate" not in result
        assert "installer" not in result
