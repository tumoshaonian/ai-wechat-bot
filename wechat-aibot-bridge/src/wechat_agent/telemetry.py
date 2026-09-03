"""Small, transport-independent runtime event interface.

The agent execution path depends only on this module.  The admin subsystem can
persist events to a database, while tests and deployments without the admin
service use the no-op implementation.  Observability failures must never make
the WeCom message path unavailable.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Protocol


LOGGER = logging.getLogger(__name__)


class EventRecorder(Protocol):
    """Synchronous event recorder safe to call from asyncio and SDK threads."""

    def record_event(
        self,
        event_type: str,
        *,
        trace_id: str | None = None,
        actor_type: str = "system",
        actor_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        severity: str = "INFO",
        idempotency_key: str | None = None,
    ) -> str | None:
        """Persist one append-only event and return its identifier when known."""

    def claim_message(self, connection_id: str, message_id: str) -> bool:
        """Atomically claim a platform message ID for durable de-duplication."""


class NullEventRecorder:
    """No-op recorder used when the admin database is disabled or unavailable."""

    def record_event(
        self,
        event_type: str,
        *,
        trace_id: str | None = None,
        actor_type: str = "system",
        actor_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        severity: str = "INFO",
        idempotency_key: str | None = None,
    ) -> str | None:
        del (
            event_type,
            trace_id,
            actor_type,
            actor_id,
            resource_type,
            resource_id,
            payload,
            severity,
            idempotency_key,
        )
        return None

    def claim_message(self, connection_id: str, message_id: str) -> bool:
        del connection_id, message_id
        return True


NULL_EVENT_RECORDER = NullEventRecorder()


def safe_record(
    recorder: EventRecorder,
    event_type: str,
    *,
    trace_id: str | None = None,
    actor_type: str = "system",
    actor_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
    severity: str = "INFO",
    idempotency_key: str | None = None,
) -> str | None:
    """Record an event without allowing telemetry failure into business flow."""

    try:
        return recorder.record_event(
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
        LOGGER.exception("Runtime event recorder failed event_type=%s", event_type)
        return None


def safe_claim_message(
    recorder: EventRecorder,
    connection_id: str,
    message_id: str,
) -> bool:
    """Use durable de-duplication, falling back to in-memory behavior on error."""

    if not message_id:
        return True
    claim = getattr(recorder, "claim_message", None)
    if not callable(claim):
        return True
    try:
        return bool(claim(connection_id, message_id))
    except Exception:
        LOGGER.exception(
            "Durable message claim failed connection_id=%s message_id=%s",
            connection_id,
            message_id,
        )
        return True
