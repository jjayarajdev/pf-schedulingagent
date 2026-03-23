"""Structured JSON logging with per-request context via contextvars."""

import json
import logging
import sys
import traceback
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_request_id: ContextVar[str] = ContextVar("request_id", default="")
_session_id: ContextVar[str] = ContextVar("session_id", default="")
_user_id: ContextVar[str] = ContextVar("user_id", default="")
_channel: ContextVar[str] = ContextVar("channel", default="")


class RequestContext:
    """Read/write per-request context fields stored in contextvars."""

    @staticmethod
    def set(
        *,
        request_id: str | None = None,
        session_id: str | None = None,
        user_id: str | None = None,
        channel: str | None = None,
    ) -> None:
        if request_id is not None:
            _request_id.set(request_id)
        if session_id is not None:
            _session_id.set(session_id)
        if user_id is not None:
            _user_id.set(user_id)
        if channel is not None:
            _channel.set(channel)

    @staticmethod
    def get_request_id() -> str:
        return _request_id.get()

    @staticmethod
    def get_session_id() -> str:
        return _session_id.get()

    @staticmethod
    def new_request_id() -> str:
        rid = str(uuid.uuid4())
        _request_id.set(rid)
        return rid

    @staticmethod
    def clear() -> None:
        _request_id.set("")
        _session_id.set("")
        _user_id.set("")
        _channel.set("")


class JSONFormatter(logging.Formatter):
    """Formats every log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = _request_id.get()
        if request_id:
            entry["request_id"] = request_id

        session_id = _session_id.get()
        if session_id:
            entry["session_id"] = session_id

        user_id = _user_id.get()
        if user_id:
            entry["user_id"] = user_id

        channel = _channel.get()
        if channel:
            entry["channel"] = channel

        if record.exc_info and record.exc_info[1]:
            entry["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else ""
            entry["exc_message"] = str(record.exc_info[1])
            entry["exc_traceback"] = traceback.format_exception(*record.exc_info)

        return json.dumps(entry, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Replace all root logger handlers with a single JSON handler on stdout."""
    root = logging.getLogger()
    root.setLevel(level)

    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)

    # Quiet down noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
