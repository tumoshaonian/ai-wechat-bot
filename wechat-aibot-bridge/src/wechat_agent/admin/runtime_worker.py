"""Durable adapter between Admin SQLite commands and Local Supervisor files."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import secrets
import socket
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


LOGGER = logging.getLogger(__name__)

COMMAND_TYPES = {"START_SERVICE", "STOP_SERVICE", "RESTART_SERVICE"}
ACTION_BY_COMMAND = {
    "START_SERVICE": "start",
    "STOP_SERVICE": "stop",
    "RESTART_SERVICE": "restart",
}
# Controlling the API from a worker hosted by that same API cannot provide a
# reliable acknowledgement.  Bridge is the only safe self-service target.
SUPPORTED_SERVICES = frozenset({"bridge"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_MAX_JSON_BYTES = 1024 * 1024


class RuntimeCommandStore(Protocol):
    def claim_control_commands(
        self,
        worker_id: str,
        *,
        command_types: set[str] | None = None,
        limit: int = 10,
        lease_seconds: int = 180,
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

    def release_control_commands(
        self,
        worker_id: str,
        *,
        command_types: set[str] | None = None,
    ) -> int: ...

    def record_event(self, event_type: str, **kwargs: Any) -> str: ...

    def project_runtime_snapshot(
        self,
        node_payload: dict[str, Any],
        service_payloads: list[dict[str, Any]],
    ) -> None: ...


class SupervisorProtocolError(RuntimeError):
    """The Supervisor files did not satisfy the documented protocol."""


class SupervisorUnavailableError(RuntimeError):
    """The Local Supervisor status is absent, invalid, or stale."""


class SupervisorCommandError(RuntimeError):
    """The Supervisor explicitly failed a service command."""


class SupervisorFileClient:
    """Strict, idempotent client for the LocalSupervisor.ps1 file queue."""

    def __init__(
        self,
        runtime_dir: Path,
        *,
        command_timeout_seconds: float = 60.0,
        status_stale_seconds: float = 15.0,
        response_poll_seconds: float = 0.1,
    ) -> None:
        self.runtime_dir = runtime_dir.expanduser().resolve()
        self.commands_dir = self.runtime_dir / "commands"
        self.processing_dir = self.runtime_dir / "processing"
        self.responses_dir = self.runtime_dir / "responses"
        self.status_path = self.runtime_dir / "status.json"
        self.command_timeout_seconds = max(2.0, command_timeout_seconds)
        self.status_stale_seconds = max(3.0, status_stale_seconds)
        self.response_poll_seconds = max(0.05, response_poll_seconds)
        for directory in (
            self.runtime_dir,
            self.commands_dir,
            self.processing_dir,
            self.responses_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def request_path(self, command_id: str) -> Path:
        self._validate_id(command_id)
        return self.commands_dir / f"db-{command_id}.json"

    def processing_path(self, command_id: str) -> Path:
        self._validate_id(command_id)
        return self.processing_dir / f"db-{command_id}.json"

    def response_path(self, command_id: str) -> Path:
        self._validate_id(command_id)
        return self.responses_dir / f"{command_id}.json"

    def read_fresh_status(self) -> dict[str, Any]:
        status = self._read_json(self.status_path, required=True)
        if not isinstance(status, dict) or status.get("schemaVersion") != 1:
            raise SupervisorProtocolError("Supervisor status has an unsupported schema")
        generated_raw = status.get("generatedAtUtc")
        if not isinstance(generated_raw, str):
            raise SupervisorProtocolError("Supervisor status is missing generatedAtUtc")
        generated = _parse_timestamp(generated_raw)
        age = (datetime.now(timezone.utc) - generated).total_seconds()
        if age < -30:
            raise SupervisorProtocolError("Supervisor status timestamp is in the future")
        if age > self.status_stale_seconds:
            raise SupervisorUnavailableError(
                f"Local Supervisor status is stale ({age:.1f} seconds old)"
            )
        supervisor = status.get("supervisor")
        if not isinstance(supervisor, dict) or supervisor.get("status") != "running":
            raise SupervisorUnavailableError("Local Supervisor is not running")
        if not isinstance(status.get("services"), dict):
            raise SupervisorProtocolError("Supervisor status is missing services")
        return status

    async def dispatch(
        self, command_id: str, action: str, service: str
    ) -> tuple[dict[str, Any], Path]:
        self._validate_command(command_id, action, service)
        response_path = self.response_path(command_id)

        # Recovery path: a previous API process may have died after Supervisor
        # completed the operation but before SQLite was updated.
        existing_response = self._read_json(response_path, required=False)
        if existing_response is not None:
            return self._validated_response(existing_response, command_id, action, service), response_path

        self.read_fresh_status()
        request_path = self.request_path(command_id)
        processing_path = self.processing_path(command_id)
        if not request_path.exists() and not processing_path.exists():
            request = {
                "id": command_id,
                "action": action,
                "service": service,
                "requestedAtUtc": datetime.now(timezone.utc).isoformat(),
                "clientPid": os.getpid(),
            }
            self._write_atomic_json(request_path, request)

        deadline = asyncio.get_running_loop().time() + self.command_timeout_seconds
        while True:
            response = self._read_json(response_path, required=False)
            if response is not None:
                return self._validated_response(response, command_id, action, service), response_path
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(
                    f"Local Supervisor did not respond within {self.command_timeout_seconds:g} seconds"
                )
            await asyncio.sleep(self.response_poll_seconds)

    @staticmethod
    def remove_response(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("Could not remove consumed Supervisor response %s", path)

    def _validated_response(
        self,
        response: Any,
        command_id: str,
        action: str,
        service: str,
    ) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise SupervisorProtocolError("Supervisor response must be a JSON object")
        if response.get("id") != command_id:
            raise SupervisorProtocolError("Supervisor response id does not match the command")
        if response.get("action") != action or response.get("service") != service:
            raise SupervisorProtocolError("Supervisor response target does not match the command")
        if response.get("success") is not True:
            error = str(response.get("error") or "Local Supervisor reported command failure")
            raise SupervisorCommandError(error)
        results = response.get("results")
        if not isinstance(results, list):
            raise SupervisorProtocolError("Supervisor response is missing step results")
        expected = 2 if action == "restart" else 1
        if len(results) != expected:
            raise SupervisorProtocolError(
                f"Supervisor {action} response must contain {expected} step result(s)"
            )
        for item in results:
            if not isinstance(item, dict) or item.get("service") != service:
                raise SupervisorProtocolError("Supervisor returned an invalid service step")
            if item.get("success") is not True:
                raise SupervisorCommandError(
                    str(item.get("message") or "A Supervisor service step failed")
                )
        statuses = [str(item.get("status") or "").lower() for item in results]
        allowed = {
            "start": [{"started", "running"}],
            "stop": [{"stopped"}],
            "restart": [{"stopped"}, {"started", "running"}],
        }[action]
        if any(status not in accepted for status, accepted in zip(statuses, allowed)):
            raise SupervisorProtocolError(
                f"Supervisor returned unexpected {action} step status: {statuses}"
            )
        return response

    @staticmethod
    def _validate_id(command_id: str) -> None:
        if not _SAFE_ID.fullmatch(command_id):
            raise ValueError("Control command id is not safe for the Supervisor queue")

    @staticmethod
    def _validate_command(command_id: str, action: str, service: str) -> None:
        SupervisorFileClient._validate_id(command_id)
        if action not in {"start", "stop", "restart"}:
            raise ValueError(f"Unsupported Supervisor action: {action}")
        if service not in SUPPORTED_SERVICES:
            raise ValueError(f"Unsupported or unsafe managed service: {service}")

    @staticmethod
    def _read_json(path: Path, *, required: bool) -> Any | None:
        try:
            stat = path.stat()
        except FileNotFoundError:
            if required:
                raise SupervisorUnavailableError(f"Supervisor file is missing: {path.name}")
            return None
        if not path.is_file() or stat.st_size > _MAX_JSON_BYTES:
            raise SupervisorProtocolError(f"Supervisor file is invalid: {path.name}")
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SupervisorProtocolError(
                f"Supervisor file is not valid JSON: {path.name}"
            ) from exc

    @staticmethod
    def _write_atomic_json(path: Path, value: Mapping[str, Any]) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
        )
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # All competing recovery writers generate the same semantic request;
            # replace keeps the visible queue file atomic on Windows and POSIX.
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


class SupervisorRuntimeCommandWorker:
    """Lease runtime commands, execute them once, and project live status."""

    def __init__(
        self,
        store: RuntimeCommandStore,
        client: SupervisorFileClient,
        *,
        poll_seconds: float = 0.75,
        lease_seconds: int = 120,
        worker_id: str | None = None,
    ) -> None:
        self._store = store
        self._client = client
        self._poll_seconds = max(0.1, poll_seconds)
        # A lease must never expire while a legitimate Supervisor operation is
        # still inside its configured response window.
        self._lease_seconds = max(
            30, lease_seconds, int(client.command_timeout_seconds) + 15
        )
        self._worker_id = worker_id or f"admin-supervisor-{secrets.token_hex(8)}"
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._store_tasks: set[asyncio.Task[Any]] = set()
        self._last_status_generation: str | None = None
        self._last_status_signature: str | None = None
        self._reported_unavailable = False

    @property
    def worker_id(self) -> str:
        return self._worker_id

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        # Complete one projection before FastAPI reports startup complete, so
        # the first /nodes or /services request cannot see an untouched
        # placeholder row from an earlier process.
        await self._sync_status(force=True)
        self._task = asyncio.create_task(
            self._run(), name=f"supervisor-runtime-{self._worker_id}"
        )

    async def close(self) -> None:
        self._stopping.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        pending_store_tasks = list(self._store_tasks)
        if pending_store_tasks:
            await asyncio.gather(*pending_store_tasks, return_exceptions=True)
        try:
            await asyncio.to_thread(
                self._store.release_control_commands,
                self._worker_id,
                command_types=COMMAND_TYPES,
            )
        except Exception:
            LOGGER.exception("Could not release Supervisor control leases during shutdown")

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._sync_status()
                commands = await self._store_call(
                    self._store.claim_control_commands,
                    self._worker_id,
                    command_types=COMMAND_TYPES,
                    # Service operations are serialized.  Leasing a batch would
                    # let later items expire while an earlier restart is still
                    # inside its graceful-stop window.
                    limit=1,
                    lease_seconds=self._lease_seconds,
                )
                for command in commands:
                    await self._execute(command)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Supervisor runtime command polling failed")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._poll_seconds
                )
            except TimeoutError:
                pass

    async def _execute(self, command: Mapping[str, Any]) -> None:
        command_id = str(command.get("id") or "")
        command_type = str(command.get("command_type") or "").upper()
        target_type = str(command.get("target_type") or "")
        service = str(command.get("target_id") or "").lower()
        try:
            if command_type not in ACTION_BY_COMMAND:
                raise ValueError(f"Unsupported runtime command: {command_type}")
            if target_type != "service" or service not in SUPPORTED_SERVICES:
                raise ValueError(f"Unsupported or unsafe runtime target: {target_type}/{service}")
            result, response_path = await self._client.dispatch(
                command_id, ACTION_BY_COMMAND[command_type], service
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.error(
                "Supervisor runtime command failed id=%s type=%s: %s",
                command_id,
                command_type,
                exc,
            )
            try:
                await self._store_call(
                    self._store.complete_control_command,
                    command_id,
                    success=False,
                    error=f"{type(exc).__name__}: {exc}",
                    worker_id=self._worker_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Keep the lease/request durable.  A replacement worker will
                # recover it once SQLite is available again.
                LOGGER.exception(
                    "Could not persist Supervisor command failure id=%s", command_id
                )
            return

        try:
            completed = await self._store_call(
                self._store.complete_control_command,
                command_id,
                success=True,
                result=result,
                worker_id=self._worker_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never turn a successful external operation into a FAILED row just
            # because its acknowledgement could not be persisted.  The response
            # file remains the recovery record for the next lease owner.
            LOGGER.exception(
                "Could not persist successful Supervisor response id=%s", command_id
            )
            return
        if not completed:
            LOGGER.warning(
                "Supervisor command completion lease was lost id=%s", command_id
            )
            return
        self._client.remove_response(response_path)
        try:
            await self._sync_status(force=True)
        except Exception:
            # Command completion is already durable; status polling on the next
            # worker iteration will repair this optional read model.
            LOGGER.exception(
                "Could not refresh Supervisor status after command id=%s", command_id
            )

    async def _sync_status(self, *, force: bool = False) -> None:
        try:
            status = await asyncio.to_thread(self._client.read_fresh_status)
        except (SupervisorProtocolError, SupervisorUnavailableError) as exc:
            if self._reported_unavailable and not force:
                return
            self._reported_unavailable = True
            self._last_status_generation = None
            transition = self._last_status_signature != "supervisor-unavailable"
            self._last_status_signature = "supervisor-unavailable"
            payload = {
                "node_id": "local",
                "name": "Local Supervisor",
                "hostname": socket.gethostname(),
                "os_name": platform.system(),
                "status": "OFFLINE",
                "capabilities": {"runtime_control": sorted(SUPPORTED_SERVICES)},
                "message": str(exc),
            }
            unavailable_services = [
                {
                    "node_id": "local",
                    "service": service_name,
                    "service_type": service_name,
                    "status": "SUPERVISOR_UNAVAILABLE",
                    "health": {"status": "unknown", "error": str(exc)},
                }
                for service_name in ("admin", "bridge")
            ]
            await self._store_call(
                self._store.project_runtime_snapshot,
                payload,
                unavailable_services,
            )
            if not transition:
                return
            await self._store_call(
                self._store.record_event,
                "node.offline",
                resource_type="node",
                resource_id="local",
                payload=payload,
                severity="WARNING",
            )
            for service_payload in unavailable_services:
                service_name = str(service_payload["service"])
                await self._store_call(
                    self._store.record_event,
                    "service.heartbeat",
                    resource_type="service",
                    resource_id=service_name,
                    payload=service_payload,
                    severity="WARNING",
                )
            return

        generated = str(status["generatedAtUtc"])
        if generated == self._last_status_generation and not force:
            return
        self._last_status_generation = generated
        self._reported_unavailable = False
        supervisor = status["supervisor"]
        node_payload = {
            "node_id": "local",
            "name": "Local Supervisor",
            "hostname": socket.gethostname(),
            "os_name": platform.system(),
            "status": "ONLINE",
            "capabilities": {
                "runtime_control": sorted(SUPPORTED_SERVICES),
                "supervisor_instance_id": supervisor.get("instanceId"),
                "supervisor_pid": supervisor.get("pid"),
            },
        }
        service_payloads: list[dict[str, Any]] = []
        signature_services: dict[str, Any] = {}
        for service_name, snapshot in status["services"].items():
            if not isinstance(snapshot, dict):
                continue
            health = snapshot.get("health")
            health = health if isinstance(health, dict) else {}
            projected_status = _service_status(snapshot, health)
            service_payloads.append(
                {
                    "node_id": "local",
                    "service": str(service_name),
                    "service_type": str(service_name),
                    "status": projected_status,
                    "pid": snapshot.get("pid"),
                    "started_at": snapshot.get("startedAtUtc"),
                    "health": {
                        **health,
                        "managed": bool(snapshot.get("managed")),
                        "enabled": bool(snapshot.get("enabled")),
                        "supervisor_status": snapshot.get("status"),
                    },
                }
            )
            signature_services[str(service_name)] = {
                "status": projected_status,
                "pid": snapshot.get("pid"),
                "managed": bool(snapshot.get("managed")),
                "enabled": bool(snapshot.get("enabled")),
                "health": health.get("status"),
                "validation_error": snapshot.get("validationError"),
            }
        await self._store_call(
            self._store.project_runtime_snapshot, node_payload, service_payloads
        )
        signature = json.dumps(
            {
                "supervisor": {
                    "status": supervisor.get("status"),
                    "instance": supervisor.get("instanceId"),
                    "pid": supervisor.get("pid"),
                },
                "services": signature_services,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        if signature == self._last_status_signature:
            return
        self._last_status_signature = signature
        await self._store_call(
            self._store.record_event,
            "node.heartbeat",
            resource_type="node",
            resource_id="local",
            payload=node_payload,
            idempotency_key=f"supervisor-status:{generated}:node",
        )
        for service_payload in service_payloads:
            service_name = str(service_payload["service"])
            await self._store_call(
                self._store.record_event,
                "service.heartbeat",
                resource_type="service",
                resource_id=str(service_name),
                payload=service_payload,
                idempotency_key=f"supervisor-status:{generated}:service:{service_name}",
            )

    async def _store_call(self, function: Any, /, *args: Any, **kwargs: Any) -> Any:
        """Run a SQLite operation without abandoning its thread on cancellation."""

        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self._store_tasks.add(task)
        task.add_done_callback(self._store_tasks.discard)
        # asyncio.to_thread itself cannot stop a running OS thread.  Shielding
        # keeps the task awaitable by close(), which then drains every database
        # call before the process/lifespan releases its files.
        return await asyncio.shield(task)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SupervisorProtocolError("Supervisor timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise SupervisorProtocolError("Supervisor timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _service_status(snapshot: Mapping[str, Any], health: Mapping[str, Any]) -> str:
    supervisor_status = str(snapshot.get("status") or "unknown").lower()
    health_status = str(health.get("status") or "").lower()
    if supervisor_status == "running":
        if health_status == "unhealthy":
            return "UNHEALTHY"
        if health_status == "healthy":
            return "HEALTHY"
        return "RUNNING"
    return supervisor_status.upper()
