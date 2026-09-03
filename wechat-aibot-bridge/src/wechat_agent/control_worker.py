"""Bridge-side consumer for administration control commands."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from .telemetry import safe_record


LOGGER = logging.getLogger(__name__)


class ControlCommandStore(Protocol):
    def claim_control_commands(
        self,
        worker_id: str,
        *,
        command_types: set[str] | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]: ...

    def complete_control_command(
        self,
        command_id: str,
        *,
        success: bool,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        worker_id: str | None = None,
    ) -> bool: ...


class AdminControlWorker:
    """Poll the durable queue and execute only commands owned by the Bridge."""

    COMMAND_TYPES = {"CANCEL_TASK", "END_SESSION", "RESEND_FILE"}

    def __init__(
        self,
        command_store: ControlCommandStore,
        backend: object,
        *,
        file_sender: object | None = None,
        poll_seconds: float = 0.75,
        worker_id: str | None = None,
    ) -> None:
        self._store = command_store
        self._backend = backend
        self._file_sender = file_sender
        self._poll_seconds = max(0.1, poll_seconds)
        self._worker_id = worker_id or f"bridge-{secrets.token_hex(6)}"
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(),
            name=f"admin-control-{self._worker_id}",
        )

    async def close(self) -> None:
        self._stopping.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                commands = await asyncio.to_thread(
                    self._store.claim_control_commands,
                    self._worker_id,
                    command_types=self.COMMAND_TYPES,
                    limit=10,
                )
                for command in commands:
                    await self._execute(command)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Admin control queue polling failed")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._poll_seconds,
                )
            except TimeoutError:
                pass

    async def _execute(self, command: Mapping[str, Any]) -> None:
        command_id = str(command.get("id") or "")
        command_type = str(command.get("command_type") or "").upper()
        target_id = str(command.get("target_id") or "")
        payload = command.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        try:
            if command_type == "CANCEL_TASK":
                result = await self._cancel_task(target_id)
            elif command_type == "END_SESSION":
                result = await self._end_session(payload)
            elif command_type == "RESEND_FILE":
                result = await self._resend_file(target_id)
            else:
                raise ValueError(f"Unsupported Bridge command: {command_type}")
        except Exception as exc:
            LOGGER.exception(
                "Admin control command failed id=%s type=%s",
                command_id,
                command_type,
            )
            await asyncio.to_thread(
                self._store.complete_control_command,
                command_id,
                success=False,
                error=str(exc),
                worker_id=self._worker_id,
            )
            return
        await asyncio.to_thread(
            self._store.complete_control_command,
            command_id,
            success=True,
            result=result,
            worker_id=self._worker_id,
        )

    async def _cancel_task(self, task_id: str) -> dict[str, object]:
        cancel = getattr(self._backend, "cancel_task", None)
        if not callable(cancel):
            raise RuntimeError("The active backend does not support task cancellation")
        result = await cancel(task_id)
        if not isinstance(result, dict):
            raise RuntimeError("Task cancellation returned an invalid result")
        if not result.get("interrupted"):
            raise RuntimeError(
                "The task is no longer running or belongs to another Bridge instance"
            )
        return result

    async def _resend_file(self, delivery_id: str) -> dict[str, object]:
        context_loader = getattr(self._store, "get_delivery_retry_context", None)
        sender = getattr(self._file_sender, "send_file_to_chat", None)
        if not callable(context_loader) or not callable(sender):
            raise RuntimeError("The active Bridge does not support file redelivery")
        context = await asyncio.to_thread(context_loader, delivery_id)
        payload = {
            "delivery_id": delivery_id,
            "artifact_id": context.get("artifact_id"),
            "task_id": context.get("task_id"),
            "trace_id": context.get("trace_id"),
            "connection_id": context.get("connection_id"),
            "retry_count": context.get("retry_count"),
        }
        safe_record(
            self._store,
            "artifact.delivery.started",
            trace_id=str(context.get("trace_id") or "") or None,
            actor_type="admin_control",
            actor_id=self._worker_id,
            resource_type="delivery",
            resource_id=delivery_id,
            payload=payload,
        )
        try:
            result = await sender(
                Path(str(context["path"])),
                str(context["external_chat_id"]),
                connection_id=str(context["connection_id"]),
            )
        except Exception as exc:
            safe_record(
                self._store,
                "artifact.delivery.failed",
                trace_id=str(context.get("trace_id") or "") or None,
                actor_type="admin_control",
                actor_id=self._worker_id,
                resource_type="delivery",
                resource_id=delivery_id,
                payload=payload | {"error": str(exc)},
                severity="ERROR",
            )
            raise
        safe_record(
            self._store,
            "artifact.delivery.succeeded",
            trace_id=str(context.get("trace_id") or "") or None,
            actor_type="admin_control",
            actor_id=self._worker_id,
            resource_type="delivery",
            resource_id=delivery_id,
            payload=payload | dict(result),
        )
        return dict(result)

    async def _end_session(self, payload: Mapping[str, Any]) -> dict[str, object]:
        session_id = str(payload.get("session_id") or "")
        if not session_id.startswith("wecom:"):
            raise ValueError("END_SESSION is missing a valid session_id")
        end = getattr(self._backend, "end_chat_session", None)
        if not callable(end):
            raise RuntimeError("The active backend does not support ending sessions")
        result = await end(session_id)
        if not isinstance(result, dict):
            raise RuntimeError("Session termination returned an invalid result")
        return result
