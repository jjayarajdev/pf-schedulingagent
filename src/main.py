"""FastAPI application — deployed on ECS Fargate via uvicorn."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.staticfiles import StaticFiles

from channels.admin import router as admin_router
from channels.chat import router as chat_router
from channels.history import router as history_router
from channels.sms import router as sms_router
from channels.vapi import router as vapi_router
from config import get_settings
from observability import RequestLoggingMiddleware, configure_logging

configure_logging()

tags_metadata = [
    {"name": "chat", "description": "Chat channel endpoints for web chat integration"},
    {"name": "vapi", "description": "Vapi phone channel webhook"},
    {"name": "sms", "description": "SMS channel via Amazon Pinpoint"},
    {"name": "conversations", "description": "Conversation history and search"},
    {"name": "admin", "description": "Admin endpoints for configuration management"},
    {"name": "system", "description": "Health and status endpoints"},
]

app = FastAPI(
    title="PF Scheduling Bot",
    description="ProjectsForce Scheduling AI Bot API",
    version="0.1.0",
    openapi_tags=tags_metadata,
)


def custom_openapi():
    """Add Bearer token security scheme so Swagger UI shows an Authorize button."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=tags_metadata,
    )
    schema["components"]["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "PF session JWT — paste your Bearer token here",
        }
    }
    schema["security"] = [{"BearerAuth": []}]
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi

app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat_router)
app.include_router(vapi_router)
app.include_router(sms_router)
app.include_router(history_router)
app.include_router(admin_router)


@app.get("/health", tags=["system"], summary="Health check")
async def health():
    settings = get_settings()
    return {
        "status": "healthy",
        "service": "scheduling-bot",
        "environment": settings.environment,
        "region": settings.aws_region,
    }


# Log startup config
settings = get_settings()
import logging as _logging
_startup_logger = _logging.getLogger("startup")
_startup_logger.info(
    "PF API: %s (environment=%s, region=%s)",
    settings.pf_api_base_url,
    settings.environment,
    settings.aws_region,
)

# Dev/QA: login proxy + test client static files
if settings.environment in ("dev", "qa") or settings.dev_server:
    import logging

    import httpx
    from pydantic import BaseModel

    _dev_logger = logging.getLogger("dev")

    class _LoginRequest(BaseModel):
        email: str
        password: str  # Pre-encrypted (CryptoJS AES with identifier as key)
        identifier: str = "projectsforce-validation"

    @app.post("/auth/login", tags=["system"], summary="Dev-only: PF login proxy")
    async def dev_login_proxy(req: _LoginRequest):
        """Proxy login to PF auth API — avoids CORS issues from test page.

        The password must be pre-encrypted (CryptoJS AES, identifier as key).
        The PF frontend encrypts passwords client-side before sending.

        Dev-only endpoint (not mounted in staging/production).
        """
        login_url = f"{settings.pf_api_base_url}/authentication/login?identifier={req.identifier}"
        _dev_logger.info("Login proxy: %s -> %s", req.email, login_url)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(login_url, json={
                "email": req.email,
                "password": req.password,
                "device_type": 1,
            })
        data = resp.json()
        if resp.status_code != 200 or not isinstance(data, dict) or "accesstoken" not in data:
            from fastapi import HTTPException
            detail = data.get("message", "Login failed") if isinstance(data, dict) else "Login failed"
            raise HTTPException(status_code=resp.status_code or 400, detail=detail)

        # Verify the token
        verify_url = f"{settings.pf_api_base_url}/authentication/verify?identifier={req.identifier}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(verify_url, json={
                    "email": req.email,
                    "accesstoken": data["accesstoken"],
                })
        except Exception:
            _dev_logger.warning("Token verify call failed (non-fatal)")

        user = data.get("user", {})
        return {
            "accesstoken": data["accesstoken"],
            "client_id": user.get("client_id", ""),
            "customer_id": str(user.get("customer_id", "")),
            "user_id": str(user.get("customer_id", "")),
            "user_name": f"{user.get('first_name', '')} {user.get('last_name', '')}".strip(),
            "email": user.get("email", req.email),
        }

    test_client_dir = Path(__file__).resolve().parent.parent / "test-client"
    if test_client_dir.exists():
        app.mount("/test", StaticFiles(directory=str(test_client_dir), html=True), name="test-client")
