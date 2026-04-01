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
_caller_type: ContextVar[str] = ContextVar("caller_type", default="customer")
_tenant_phone: ContextVar[str] = ContextVar("tenant_phone", default="")
_timezone: ContextVar[str] = ContextVar("timezone", default="US/Eastern")
_support_number: ContextVar[str] = ContextVar("support_number", default="")
_support_email: ContextVar[str] = ContextVar("support_email", default="")
_office_hours: ContextVar[list] = ContextVar("office_hours", default=[])


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
        caller_type: str | None = None,
        tenant_phone: str | None = None,
        timezone: str | None = None,
        support_number: str | None = None,
        support_email: str | None = None,
        office_hours: list | None = None,
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
        if caller_type is not None:
            _caller_type.set(caller_type)
        if tenant_phone is not None:
            _tenant_phone.set(tenant_phone)
        if timezone is not None:
            _timezone.set(timezone)
        if support_number is not None:
            _support_number.set(support_number)
        if support_email is not None:
            _support_email.set(support_email)
        if office_hours is not None:
            _office_hours.set(office_hours)

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
    def get_caller_type() -> str:
        return _caller_type.get()

    @staticmethod
    def get_tenant_phone() -> str:
        return _tenant_phone.get()

    @staticmethod
    def get_timezone() -> str:
        return _timezone.get()

    @staticmethod
    def get_support_number() -> str:
        return _support_number.get()

    @staticmethod
    def get_support_email() -> str:
        return _support_email.get()

    @staticmethod
    def get_office_hours() -> list:
        return _office_hours.get()

    @staticmethod
    def clear() -> None:
        _auth_token.set("")
        _client_id.set("")
        _customer_id.set("")
        _user_id.set("")
        _user_name.set("")
        _caller_type.set("customer")
        _tenant_phone.set("")
        _timezone.set("US/Eastern")
        _support_number.set("")
        _support_email.set("")
        _office_hours.set([])
