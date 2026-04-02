"""Admin endpoints for managing Vapi assistant configuration.

Provides CRUD operations for the Vapi assistant → phone number mapping
used by the phone channel to resolve ``to_phone`` for PF authentication.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from auth.phone_auth import delete_cached_creds
from channels.vapi_config import delete_assistant, list_assistants, register_assistant

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


class AssistantRegistration(BaseModel):
    """Request body for registering a Vapi assistant."""

    assistant_id: str
    phone_number: str
    tenant_name: str = ""
    support_number: str = ""


@router.get("/vapi-assistants", summary="List registered Vapi assistants")
async def get_vapi_assistants():
    """Return all registered Vapi assistant → phone number mappings."""
    items = list_assistants()
    return {"count": len(items), "assistants": items}


@router.post("/vapi-assistants", summary="Register a Vapi assistant", status_code=201)
async def create_vapi_assistant(body: AssistantRegistration):
    """Register or update a Vapi assistant → phone number mapping."""
    if not body.assistant_id or not body.phone_number:
        raise HTTPException(status_code=400, detail="assistant_id and phone_number are required")

    try:
        register_assistant(body.assistant_id, body.phone_number, body.tenant_name, body.support_number)
    except Exception as exc:
        logger.exception("Failed to register Vapi assistant")
        raise HTTPException(status_code=500, detail="Failed to register assistant") from exc

    return {
        "assistant_id": body.assistant_id,
        "phone_number": body.phone_number,
        "tenant_name": body.tenant_name,
        "support_number": body.support_number,
    }


@router.delete(
    "/vapi-assistants/{assistant_id}",
    summary="Remove a Vapi assistant config",
)
async def remove_vapi_assistant(assistant_id: str):
    """Delete a Vapi assistant → phone number mapping."""
    deleted = delete_assistant(assistant_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Assistant not found or delete failed")
    return {"deleted": assistant_id}


@router.delete(
    "/phone-cache/{phone}",
    summary="Flush cached phone credentials",
)
async def flush_phone_cache(phone: str):
    """Delete DynamoDB-cached auth credentials for a phone number.

    Useful for testing — forces the next call from this number to
    re-authenticate via the PF phone-call-login API.
    """
    deleted = delete_cached_creds(phone)
    if not deleted:
        raise HTTPException(status_code=400, detail="Invalid phone number or delete failed")
    return {"flushed": phone}
