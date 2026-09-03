"""Isolated, non-persistent enterprise WeChat credential verification.

The probe deliberately creates a short-lived SDK client that is unrelated to the
Bridge's live client.  It never exposes credentials in its result and always tries
to disconnect, including after timeout or authentication failure.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .redaction import redact_text


_NOT_RUN = {"status": "NOT_RUN"}


@dataclass(frozen=True)
class ConnectionProbeResult:
    """Public, credential-free result of one isolated probe."""

    ok: bool
    code: str
    phase: str
    message: str
    stages: dict[str, dict[str, str]]
    duration_ms: int

    def public(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "phase": self.phase,
            "message": self.message,
            "stages": {name: dict(value) for name, value in self.stages.items()},
            "duration_ms": self.duration_ms,
        }


class ConnectionProbe(Protocol):
    async def probe(
        self, bot_id: str, secret: str, *, timeout_seconds: float = 12.0
    ) -> ConnectionProbeResult: ...


class WeComCredentialProbe:
    """Verify credentials through a disposable ``WSClient`` instance.

    ``client_factory`` exists solely to keep tests deterministic and offline.  In
    production the SDK is imported lazily so the admin API can still boot and
    report a precise SDK-stage failure when the optional package is unavailable.
    """

    def __init__(self, client_factory: Callable[..., Any] | None = None) -> None:
        self._client_factory = client_factory

    async def probe(
        self, bot_id: str, secret: str, *, timeout_seconds: float = 12.0
    ) -> ConnectionProbeResult:
        started = time.monotonic()
        stages = _new_stages()
        if not bot_id or not secret:
            stages["configuration"] = {
                "status": "FAILED",
                "code": "CONNECTION_INCOMPLETE",
            }
            return _result(
                False,
                "CONNECTION_INCOMPLETE",
                "configuration",
                "Bot ID and Secret are required.",
                stages,
                started,
            )
        stages["configuration"] = {"status": "SUCCEEDED"}

        try:
            factory = self._client_factory or _load_sdk_client()
            # One auth attempt is enough for a validation probe.  Retrying bad
            # credentials only increases latency and never changes the outcome.
            client = factory(
                bot_id,
                secret,
                max_auth_failure_attempts=1,
                max_reconnect_attempts=0,
            )
        except ImportError:
            stages["sdk"] = {"status": "FAILED", "code": "SDK_UNAVAILABLE"}
            return _result(
                False,
                "SDK_UNAVAILABLE",
                "sdk",
                "The enterprise WeChat SDK is not installed.",
                stages,
                started,
            )
        except Exception as exc:
            stages["sdk"] = {"status": "FAILED", "code": "SDK_INITIALIZATION_FAILED"}
            return _result(
                False,
                "SDK_INITIALIZATION_FAILED",
                "sdk",
                _safe_error(exc, bot_id, secret),
                stages,
                started,
            )

        stages["sdk"] = {"status": "SUCCEEDED"}
        loop = asyncio.get_running_loop()
        outcome: asyncio.Future[tuple[bool, str, str, str]] = loop.create_future()
        network_connected = False

        def finish(ok: bool, code: str, phase: str, message: str) -> None:
            if not outcome.done():
                outcome.set_result((ok, code, phase, message))

        def on_connected() -> None:
            nonlocal network_connected
            network_connected = True
            stages["network"] = {"status": "SUCCEEDED"}

        def on_authenticated() -> None:
            stages["network"] = {"status": "SUCCEEDED"}
            stages["authentication"] = {"status": "SUCCEEDED"}
            finish(True, "AUTHENTICATED", "authentication", "Authentication succeeded.")

        def on_error(error: object) -> None:
            message = _safe_error(error, bot_id, secret)
            authentication_error = _is_authentication_error(error)
            phase = "authentication" if authentication_error or network_connected else "network"
            code = "AUTHENTICATION_FAILED" if phase == "authentication" else "NETWORK_ERROR"
            if network_connected:
                stages["network"] = {"status": "SUCCEEDED"}
            else:
                stages["network"] = {"status": "FAILED", "code": code}
            if phase == "authentication":
                stages["authentication"] = {"status": "FAILED", "code": code}
            finish(False, code, phase, message)

        def on_disconnected(reason: object = None) -> None:
            if not outcome.done():
                message = _safe_error(reason or "Connection closed before authentication", bot_id, secret)
                phase = "authentication" if network_connected else "network"
                code = "AUTHENTICATION_NOT_COMPLETED" if network_connected else "CONNECTION_CLOSED"
                stages["network"] = {"status": "SUCCEEDED"} if network_connected else {
                    "status": "FAILED",
                    "code": code,
                }
                if network_connected:
                    stages["authentication"] = {"status": "FAILED", "code": code}
                finish(False, code, phase, message)

        client.on("connected", on_connected)
        client.on("authenticated", on_authenticated)
        client.on("error", on_error)
        client.on("disconnected", on_disconnected)

        connect_task: asyncio.Task[Any] | None = None
        try:
            connect_task = asyncio.create_task(client.connect())

            def connect_done(task: asyncio.Task[Any]) -> None:
                if task.cancelled():
                    return
                exception = task.exception()
                if exception is not None:
                    on_error(exception)

            connect_task.add_done_callback(connect_done)
            try:
                ok, code, phase, message = await asyncio.wait_for(
                    asyncio.shield(outcome), timeout=max(0.1, timeout_seconds)
                )
            except TimeoutError:
                phase = "authentication" if network_connected else "network"
                code = "AUTHENTICATION_TIMEOUT" if network_connected else "NETWORK_TIMEOUT"
                stages["network"] = {"status": "SUCCEEDED"} if network_connected else {
                    "status": "FAILED",
                    "code": code,
                }
                if network_connected:
                    stages["authentication"] = {"status": "FAILED", "code": code}
                ok, message = False, "The credential probe timed out before authentication completed."
            return _result(ok, code, phase, message, stages, started)
        finally:
            if connect_task is not None and not connect_task.done():
                connect_task.cancel()
                await asyncio.gather(connect_task, return_exceptions=True)
            try:
                await asyncio.wait_for(client.disconnect(), timeout=5.0)
            except BaseException:
                # Cleanup must never replace the actual probe result.  In
                # particular, do not log an SDK exception that could echo input.
                pass


def _load_sdk_client() -> Callable[..., Any]:
    from wecom_aibot_sdk import WSClient

    return WSClient


def _new_stages() -> dict[str, dict[str, str]]:
    return {
        "configuration": dict(_NOT_RUN),
        "sdk": dict(_NOT_RUN),
        "network": dict(_NOT_RUN),
        "authentication": dict(_NOT_RUN),
    }


def _result(
    ok: bool,
    code: str,
    phase: str,
    message: str,
    stages: dict[str, dict[str, str]],
    started: float,
) -> ConnectionProbeResult:
    return ConnectionProbeResult(
        ok=ok,
        code=code,
        phase=phase,
        message=redact_text(str(message), max_length=1000),
        stages=stages,
        duration_ms=max(0, round((time.monotonic() - started) * 1000)),
    )


def _safe_error(error: object, bot_id: str, secret: str) -> str:
    text = str(error) or type(error).__name__
    # SDK/network exception messages are outside our control.  Explicitly remove
    # both supplied values before applying the central pattern-based redactor.
    for value in sorted({bot_id, secret}, key=len, reverse=True):
        if value:
            text = text.replace(value, "***")
    return redact_text(text, max_length=1000)


def _is_authentication_error(error: object) -> bool:
    code = str(getattr(error, "code", "")).upper()
    text = f"{type(error).__name__} {error}".lower()
    return "AUTH" in code or any(
        marker in text
        for marker in ("authentication", "authenticate", "credential", "unauthorized", "forbidden")
    )
