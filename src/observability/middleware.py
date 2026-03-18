"""Request logging middleware — generates request_id, logs start/end, tracks latency."""

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from auth.context import AuthContext
from observability.logging import RequestContext

logger = logging.getLogger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware that wraps each HTTP request with structured context."""

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = RequestContext.new_request_id()
        channel = _channel_from_path(request.url.path)
        RequestContext.set(channel=channel)

        logger.info(
            "Request started: %s %s",
            request.method,
            request.url.path,
            extra={"http_method": request.method, "path": request.url.path},
        )

        start = time.monotonic()
        try:
            response = await call_next(request)
            latency_ms = round((time.monotonic() - start) * 1000)

            logger.info(
                "Request completed: %s %s → %d (%dms)",
                request.method,
                request.url.path,
                response.status_code,
                latency_ms,
            )

            response.headers["X-Request-ID"] = request_id
            return response

        except Exception:
            latency_ms = round((time.monotonic() - start) * 1000)
            logger.exception(
                "Request failed: %s %s (%dms)",
                request.method,
                request.url.path,
                latency_ms,
            )
            raise
        finally:
            RequestContext.clear()
            AuthContext.clear()


def _channel_from_path(path: str) -> str:
    """Derive channel name from request path."""
    if path.startswith("/chat"):
        return "chat"
    if path.startswith("/vapi"):
        return "vapi"
    if path.startswith("/sms"):
        return "sms"
    return "system"
