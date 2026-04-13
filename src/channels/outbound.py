"""Outbound call endpoints — status, manual trigger, call listing."""

import logging

from fastapi import APIRouter, Depends, HTTPException

from channels.outbound_store import get_calls_for_project, get_outbound_call
from channels.schemas import OutboundTriggerRequest
from channels.vapi import verify_vapi_secret

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/outbound", tags=["Outbound Calls"])


@router.get(
    "/{call_id}/status",
    summary="Get outbound call status",
    description="Check the status and outcome of an outbound call. Used by PF backend to poll call results.",
)
async def get_status(call_id: str) -> dict:
    call = await get_outbound_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    return {
        "call_id": call["call_id"],
        "project_id": call.get("project_id", ""),
        "status": call.get("status", "unknown"),
        "attempt_number": call.get("attempt_number", 0),
        "phone_used": call.get("phone_used", ""),
        "vapi_call_id": call.get("vapi_call_id", ""),
        "call_result": call.get("call_result"),
        "created_at": call.get("created_at", ""),
        "updated_at": call.get("updated_at", ""),
    }


@router.get(
    "/calls",
    summary="List outbound calls",
    description="List outbound calls, optionally filtered by project_id.",
)
async def list_calls(project_id: str = "") -> dict:
    if not project_id:
        return {"calls": [], "message": "Provide project_id to filter calls"}
    calls = await get_calls_for_project(project_id)
    return {
        "project_id": project_id,
        "calls": calls,
        "count": len(calls),
    }


@router.post(
    "/trigger",
    summary="Manually trigger outbound call",
    description=(
        "Trigger an outbound scheduling call manually (for dev/testing). "
        "Production flow uses SQS consumer. "
        "Auth: x-vapi-secret header."
    ),
    dependencies=[Depends(verify_vapi_secret)],
)
async def manual_trigger(request: OutboundTriggerRequest) -> dict:
    """Manual trigger — bypasses SQS, directly initiates a call."""
    from channels.outbound_consumer import process_trigger

    logger.info(
        "Manual trigger: project=%s client=%s phone=%s",
        request.project_id,
        request.client_id,
        request.customer.primary_phone,
    )
    try:
        result = await process_trigger(request.model_dump())
        return result
    except Exception as exc:
        logger.exception("Manual trigger failed: project=%s", request.project_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
