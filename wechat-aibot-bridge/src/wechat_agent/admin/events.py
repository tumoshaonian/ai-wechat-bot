"""Best-effort, non-blocking-from-the-caller event persistence facade."""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, is_dataclass
from typing import Any

from .config import AdminSettings
from .security import SecretBox
from .store import AdminStore


LOGGER = logging.getLogger(__name__)
_RECORDER: "AdminEventRecorder | None" = None
_RECORDER_LOCK = threading.Lock()


class AdminEventRecorder:
    """Persist operational facts without allowing admin failures to break the bot."""

    def __init__(self, store: AdminStore | None, *, initialization_error: Exception | None = None) -> None:
        self.store = store
        self.initialization_error = initialization_error

    @property
    def available(self) -> bool:
        return self.store is not None

    @classmethod
    def from_environment(cls) -> "AdminEventRecorder":
        try:
            settings = AdminSettings.from_environment()
            store = AdminStore(
                settings.database_path,
                SecretBox.load(settings.master_key_path),
                reconcile_on_start=True,
            )
            return cls(store)
        except Exception as exc:  # pragma: no cover - exercised by deployment failures
            LOGGER.exception("Admin event persistence is unavailable; Bridge will continue")
            return cls(None, initialization_error=exc)

    def claim_message(self, connection_id: str, message_id: str) -> bool:
        """Return False only for a proven duplicate; storage errors fail open."""

        if self.store is None:
            return True
        try:
            return self.store.claim_message(connection_id, message_id)
        except Exception:
            LOGGER.exception("Could not persist message claim; using Bridge in-memory fallback")
            return True

    def record_event(
        self,
        event_type: str,
        *,
        trace_id: str | None = None,
        actor_type: str = "system",
        actor_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        payload: dict[str, Any] | None = None,
        severity: str = "INFO",
        idempotency_key: str | None = None,
    ) -> str | None:
        if self.store is None:
            return None
        try:
            return self.store.record_event(
                event_type,
                trace_id=trace_id,
                actor_type=actor_type,
                actor_id=actor_id,
                resource_type=resource_type,
                resource_id=resource_id,
                payload=payload,
                severity=severity,
                idempotency_key=idempotency_key,
            )
        except Exception:
            LOGGER.exception("Could not persist admin event type=%s", event_type)
            return None

    def record_inbound_message(self, message: Any, *, connection_id: str = "default", task_id: str | None = None, trace_id: str | None = None) -> str | None:
        payload = _message_payload(message)
        payload.update(connection_id=connection_id, task_id=task_id, trace_id=trace_id)
        return self.record_event(
            "message.received", trace_id=trace_id, actor_type="wecom_user",
            actor_id=payload.get("sender_id"), resource_type="message",
            resource_id=payload.get("message_id"), payload=payload,
            idempotency_key=f"message.received:{connection_id}:{payload.get('message_id')}",
        )

    def record_outbound_message(self, message: Any | None = None, *, connection_id: str = "default", task_id: str | None = None, trace_id: str | None = None, content: str = "", status: str = "SENT", **extra: Any) -> str | None:
        payload = _message_payload(message) if message is not None else {}
        payload.update(connection_id=connection_id, task_id=task_id, trace_id=trace_id, content=content, status=status, **extra)
        return self.record_event("message.outbound", trace_id=trace_id, resource_type="message", payload=payload)

    def record_task_started(self, *, task_id: str, trace_id: str, payload: dict[str, Any] | None = None) -> str | None:
        body = dict(payload or {})
        body.update(task_id=task_id, trace_id=trace_id)
        return self.record_event("task.started", trace_id=trace_id, resource_type="task", resource_id=task_id, payload=body, idempotency_key=f"task.started:{task_id}")

    def record_task_finished(self, *, task_id: str, trace_id: str, status: str, payload: dict[str, Any] | None = None) -> str | None:
        event_type = {"SUCCEEDED": "task.completed", "FAILED": "task.failed", "CANCELLED": "task.cancelled", "TIMED_OUT": "task.timeout"}.get(status.upper(), "task.completed")
        body = dict(payload or {})
        body.update(task_id=task_id, trace_id=trace_id, status=status.upper())
        return self.record_event(event_type, trace_id=trace_id, resource_type="task", resource_id=task_id, payload=body, idempotency_key=f"{event_type}:{task_id}")

    def claim_control_commands(self, worker_id: str, *, command_types: set[str] | None = None, limit: int = 10, lease_seconds: int = 180) -> list[dict[str, Any]]:
        if self.store is None:
            return []
        try:
            return self.store.claim_control_commands(
                worker_id, command_types=command_types, limit=limit,
                lease_seconds=lease_seconds,
            )
        except Exception:
            LOGGER.exception("Could not claim admin control commands")
            return []

    def complete_control_command(self, command_id: str, *, success: bool, result: dict[str, Any] | None = None, error: str | None = None, worker_id: str | None = None) -> bool:
        if self.store is None:
            return False
        try:
            return self.store.complete_control_command(
                command_id, success=success, result=result, error=error,
                worker_id=worker_id,
            )
        except Exception:
            LOGGER.exception("Could not complete admin control command id=%s", command_id)
            return False

    def get_active_connection_credentials(self) -> dict[str, str] | None:
        if self.store is None:
            return None
        try:
            return self.store.get_active_connection_credentials()
        except Exception:
            LOGGER.exception("Could not load active connection credentials")
            return None

    def ensure_environment_connection(self, connection_id: str, bot_id: str, secret: str) -> dict[str, str] | None:
        if self.store is None:
            return None
        try:
            return self.store.ensure_environment_connection(
                connection_id, bot_id, secret
            )
        except Exception:
            # Deliberately omit every credential value from diagnostics.
            LOGGER.exception("Could not import environment WeCom connection")
            return None

    def get_delivery_retry_context(self, delivery_id: str) -> dict[str, Any]:
        if self.store is None:
            raise RuntimeError("Administration storage is unavailable")
        return self.store.get_delivery_retry_context(delivery_id)

    def authorize_wecom_user(
        self,
        connection_id: str,
        external_user_id: str,
        *,
        bootstrap_allowed: bool,
    ) -> tuple[bool, str]:
        if self.store is None:
            return bootstrap_allowed, "storage_unavailable"
        try:
            return self.store.authorize_wecom_user(
                connection_id,
                external_user_id,
                bootstrap_allowed=bootstrap_allowed,
            )
        except Exception:
            LOGGER.exception("Could not evaluate the WeCom user access policy")
            return bootstrap_allowed, "storage_error"

    def get_wecom_user_policy(
        self,
        connection_id: str,
        external_user_id: str,
    ) -> dict[str, Any]:
        if self.store is None:
            return {}
        try:
            return self.store.get_wecom_user_policy(
                connection_id,
                external_user_id,
            )
        except Exception:
            LOGGER.exception("Could not load the WeCom user capability policy")
            return {}

    def get_active_runtime_config(self) -> dict[str, Any] | None:
        if self.store is None:
            return None
        try:
            return self.store.get_active_runtime_config()
        except Exception:
            LOGGER.exception("Could not load the published Agent configuration")
            return None


def get_event_recorder() -> AdminEventRecorder:
    global _RECORDER
    if _RECORDER is None:
        with _RECORDER_LOCK:
            if _RECORDER is None:
                _RECORDER = AdminEventRecorder.from_environment()
    return _RECORDER


def set_event_recorder_for_tests(recorder: AdminEventRecorder | None) -> None:
    global _RECORDER
    with _RECORDER_LOCK:
        _RECORDER = recorder


def _message_payload(message: Any) -> dict[str, Any]:
    if is_dataclass(message):
        raw = asdict(message)
    elif isinstance(message, dict):
        raw = dict(message)
    else:
        raw = {
            name: getattr(message, name)
            for name in ("message_id", "sender_id", "chat_id", "chat_type", "content", "session_id", "task_id", "trace_id")
            if hasattr(message, name)
        }
    return raw
