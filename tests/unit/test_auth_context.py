"""Tests for AuthContext contextvars management."""

from auth.context import AuthContext


class TestAuthContext:
    def test_defaults_are_empty(self):
        assert AuthContext.get_auth_token() == ""
        assert AuthContext.get_client_id() == ""
        assert AuthContext.get_customer_id() == ""
        assert AuthContext.get_user_id() == ""
        assert AuthContext.get_user_name() == ""

    def test_set_all_fields(self):
        AuthContext.set(
            auth_token="tok",
            client_id="cid",
            customer_id="cust",
            user_id="uid",
            user_name="John Doe",
        )
        assert AuthContext.get_auth_token() == "tok"
        assert AuthContext.get_client_id() == "cid"
        assert AuthContext.get_customer_id() == "cust"
        assert AuthContext.get_user_id() == "uid"
        assert AuthContext.get_user_name() == "John Doe"

    def test_set_partial(self):
        AuthContext.set(auth_token="tok", client_id="cid")
        assert AuthContext.get_auth_token() == "tok"
        assert AuthContext.get_client_id() == "cid"
        assert AuthContext.get_customer_id() == ""  # Not set

    def test_clear(self):
        AuthContext.set(auth_token="tok", client_id="cid", user_name="Name")
        AuthContext.clear()
        assert AuthContext.get_auth_token() == ""
        assert AuthContext.get_client_id() == ""
        assert AuthContext.get_user_name() == ""

    def test_set_none_does_not_clear(self):
        AuthContext.set(auth_token="tok")
        AuthContext.set(auth_token=None)  # Should NOT clear
        assert AuthContext.get_auth_token() == "tok"
