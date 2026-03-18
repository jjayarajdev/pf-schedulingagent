"""Auth context — JWT, client_id, customer_id, user_id, and user_name via contextvars.

Set by channel endpoints before calling orchestrator.route_request().
Read by tool handlers that need to call PF internal APIs on behalf of the user.
"""

from contextvars import ContextVar

_auth_token: ContextVar[str] = ContextVar("auth_token", default="")
_client_id: ContextVar[str] = ContextVar("client_id", default="")
_customer_id: ContextVar[str] = ContextVar("customer_id", default="")
_user_id: ContextVar[str] = ContextVar("user_id", default="")
_user_name: ContextVar[str] = ContextVar("user_name", default="")


class AuthContext:
    """Read/write per-request auth fields stored in contextvars."""

    @staticmethod
    def set(
        *,
        auth_token: str | None = None,
        client_id: str | None = None,
        customer_id: str | None = None,
        user_id: str | None = None,
        user_name: str | None = None,
    ) -> None:
        if auth_token is not None:
            _auth_token.set(auth_token)
        if client_id is not None:
            _client_id.set(client_id)
        if customer_id is not None:
            _customer_id.set(customer_id)
        if user_id is not None:
            _user_id.set(user_id)
        if user_name is not None:
            _user_name.set(user_name)

    @staticmethod
    def get_auth_token() -> str:
        return _auth_token.get()

    @staticmethod
    def get_client_id() -> str:
        return _client_id.get()

    @staticmethod
    def get_customer_id() -> str:
        return _customer_id.get()

    @staticmethod
    def get_user_id() -> str:
        return _user_id.get()

    @staticmethod
    def get_user_name() -> str:
        return _user_name.get()

    @staticmethod
    def clear() -> None:
        _auth_token.set("")
        _client_id.set("")
        _customer_id.set("")
        _user_id.set("")
        _user_name.set("")
