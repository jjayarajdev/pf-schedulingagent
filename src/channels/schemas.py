"""Request/response schemas for the scheduling AI bot channels."""

from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """Incoming chat message.

    Accepts both canonical field names (``auth_token``, ``client_id``, etc.)
    and the ``pf_``-prefixed names sent by the PF web app (``pf_token``,
    ``pf_client_id``, ``pf_user_id``, ``pf_user_name``).
    """

    message: str = Field(..., min_length=1, max_length=2000, description="User's question or message")
    session_id: str = Field(default="", description="Session ID for conversation continuity")
    user_id: str = Field(default="", description="User identifier")
    auth_token: str = Field(default="", description="PF session token for API passthrough")
    client_id: str = Field(default="", description="PF tenant client_id")
    customer_id: str = Field(default="", description="PF customer_id")
    user_name: str = Field(default="", description="Display name for personalized greetings")
    client_name: str = Field(default="", description="PF tenant name (e.g. 'projectsforce-validation')")
    stream: bool = Field(default=False, description="Whether to stream the response (ignored by non-stream endpoint)")

    # PF web app sends pf_-prefixed field names
    pf_token: str = Field(default="", description="Alias for auth_token (sent by PF web app)")
    pf_client_id: str = Field(default="", description="Alias for client_id (sent by PF web app)")
    pf_user_id: str = Field(default="", description="Alias for user_id (sent by PF web app)")
    pf_user_name: str = Field(default="", description="Alias for user_name (sent by PF web app)")


class ChatResponse(BaseModel):
    """Chat API response — matches v1.2.9 response contract.

    Fields ``pf_http_status_code`` and ``agenticscheduler_http_status_code``
    are required by the frontend for session-expiry detection (401/403)
    and error handling.
    """

    response: str
    session_id: str
    agent_name: str = Field(default="", description="Name of the agent that handled this request")
    intent: str = Field(default="", description="Classified intent (scheduling, chitchat, welcome, etc.)")
    action: str | None = Field(default=None, description="Action performed (list_projects, schedule_project, etc.)")
    pf_http_status_code: int | None = Field(default=200, description="PF API HTTP status. Frontend uses 401/403 for re-login.")
    agenticscheduler_http_status_code: int = Field(default=200, description="Bot-level HTTP status (200 or 500)")
    projects: list[dict[str, Any]] | None = Field(default=None, description="Project list (populated on welcome flow)")
    confirmation_required: bool | None = Field(default=None, description="True when user must confirm an appointment action")
    pending_action: dict[str, Any] | None = Field(default=None, description="Appointment details awaiting confirmation")


class SmsInboundPayload(BaseModel):
    """Inbound SMS message from AWS Pinpoint via SNS."""

    originationNumber: str = Field(..., description="Sender's phone number")
    messageBody: str = Field(..., description="SMS message text")
    destinationNumber: str = Field(default="", description="Receiving phone number")


class VapiPayload(BaseModel):
    """Vapi webhook payload (flexible — Vapi sends various event types)."""

    message: dict = Field(default_factory=dict, description="Vapi event message object")


class OutboundCustomer(BaseModel):
    """Customer info in outbound trigger — matches PF backend payload."""

    customer_id: str = ""
    first_name: str = ""
    last_name: str = ""
    primary_phone: str = Field(..., description="Primary phone (E.164)")
    alternate_phone: str = Field(default="", description="Alternate phone (E.164)")


class OutboundTenantInfo(BaseModel):
    """Tenant/project classification — matches PF backend payload."""

    client_id: str = ""
    source: str | None = None
    category: str | None = None
    type: str | None = None


class OutboundProject(BaseModel):
    """Project summary — matches PF backend payload."""

    project_number: str | None = None
    status_id: str | None = None
    status_name: str | None = None


class OutboundTriggerRequest(BaseModel):
    """Trigger for outbound call — matches PF backend SQS payload."""

    event: str = "auto_call_ready"
    project_id: str
    client_id: str
    customer_id: str = ""
    store_id: str = ""
    customer: OutboundCustomer
    tenant_info: OutboundTenantInfo = Field(default_factory=OutboundTenantInfo)
    project: OutboundProject = Field(default_factory=OutboundProject)
    vapi_phone_number_id: str = Field(default="", description="Vapi phone to call FROM")
    metadata: dict = Field(default_factory=dict)
