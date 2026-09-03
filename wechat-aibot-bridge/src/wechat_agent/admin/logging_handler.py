"""Safe Python logging bridge into the structured admin event stream."""

from __future__ import annotations

import logging
import threading
from typing import Any

from .events import AdminEventRecorder, get_event_recorder
from .redaction import redact_text


class AdminLogHandler(logging.Handler):
    def __init__(self, recorder: AdminEventRecorder, *, service: str = "bridge") -> None:
        super().__init__()
        self.recorder = recorder
        self.service = service
        self._local = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._local, "active", False):
            return
        # Never ingest our own persistence diagnostics; that would recurse on failure.
        if record.name.startswith("wechat_agent.admin"):
            return
        self._local.active = True
        try:
            message = redact_text(self.format(record))
            payload: dict[str, Any] = {
                "service": getattr(record, "service", self.service),
                "logger": record.name,
                "message": message,
            }
            self.recorder.record_event(
                "log.python",
                trace_id=getattr(record, "trace_id", None),
                resource_type="log",
                severity=record.levelname,
                payload=payload,
            )
        except Exception:
            # Logging must never fail the message-processing path.
            pass
        finally:
            self._local.active = False


def install_database_log_handler(
    recorder: AdminEventRecorder | None = None,
    *,
    service: str = "bridge",
    logger: logging.Logger | None = None,
) -> AdminLogHandler:
    target = logger or logging.getLogger()
    for handler in target.handlers:
        if isinstance(handler, AdminLogHandler) and handler.service == service:
            return handler
    handler = AdminLogHandler(recorder or get_event_recorder(), service=service)
    handler.setFormatter(logging.Formatter("%(message)s"))
    target.addHandler(handler)
    return handler
