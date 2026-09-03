"""Python boundary for the bundled PowerShell Windows UI Automation adapter."""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)

DEFAULT_ACTION_TIMEOUT_SECONDS = 180.0
DEFAULT_PREFLIGHT_TIMEOUT_SECONDS = 25.0
DEFAULT_DOUBAO_WINDOW_TIMEOUT_SECONDS = 30.0
DEFAULT_DOUBAO_ANSWER_TIMEOUT_SECONDS = 120.0
DEFAULT_DOUBAO_STABLE_SECONDS = 4.0
MAX_DOUBAO_WINDOW_TIMEOUT_SECONDS = 30.0
MAX_DOUBAO_ANSWER_TIMEOUT_SECONDS = 120.0
MAX_DOUBAO_STABLE_SECONDS = 30.0
DOUBAO_COORDINATION_GRACE_SECONDS = 30.0
DOUBAO_MAX_REQUEST_BUDGET_SECONDS = (
    MAX_DOUBAO_WINDOW_TIMEOUT_SECONDS
    + MAX_DOUBAO_ANSWER_TIMEOUT_SECONDS
    + DOUBAO_COORDINATION_GRACE_SECONDS
)


class DesktopWorkerError(RuntimeError):
    """Raised when the native UI Automation adapter cannot complete an action."""


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class DesktopWorker:
    """Execute typed desktop actions through one reviewed Windows adapter script."""

    def __init__(
        self,
        *,
        powershell_bin: str,
        script_path: Path,
        timeout_seconds: float = DEFAULT_ACTION_TIMEOUT_SECONDS,
        preflight_timeout_seconds: float = DEFAULT_PREFLIGHT_TIMEOUT_SECONDS,
        screenshot_directory: Path,
        doubao_launch_path: Path | None = None,
        runner: CommandRunner = subprocess.run,
    ) -> None:
        self._powershell_bin = powershell_bin
        self._script_path = script_path.resolve()
        self._timeout_seconds = _positive_finite_number(
            timeout_seconds,
            name="timeout_seconds",
        )
        if self._timeout_seconds < DOUBAO_MAX_REQUEST_BUDGET_SECONDS:
            raise ValueError(
                "timeout_seconds must be at least "
                f"{DOUBAO_MAX_REQUEST_BUDGET_SECONDS:g}s so the advertised "
                "doubao_ask timeout range can be honored"
            )
        self._preflight_timeout_seconds = _positive_finite_number(
            preflight_timeout_seconds,
            name="preflight_timeout_seconds",
        )
        self._screenshot_directory = screenshot_directory.resolve()
        self._doubao_launch_path = doubao_launch_path.resolve() if doubao_launch_path else None
        self._runner = runner

    def execute(
        self,
        action: str,
        payload: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Run one native action and return its structured JSON result."""

        request = dict(payload or {})
        request["_worker_pid"] = os.getpid()
        encoded = base64.b64encode(
            json.dumps(request, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        command = [
            self._powershell_bin,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self._script_path),
            "-Action",
            action,
            "-PayloadBase64",
            encoded,
        ]
        effective_timeout = timeout_seconds or self._timeout_seconds
        LOGGER.info("Desktop action started action=%s timeout=%ss", action, effective_timeout)
        try:
            completed = self._runner(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective_timeout,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise DesktopWorkerError(
                f"desktop action timed out after {effective_timeout:g}s: {action}"
            ) from exc
        except OSError as exc:
            raise DesktopWorkerError(f"could not start desktop adapter: {exc}") from exc

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if completed.returncode != 0:
            detail = stderr or stdout or f"exit code {completed.returncode}"
            LOGGER.error("Desktop action failed action=%s detail=%s", action, detail)
            raise DesktopWorkerError(detail)
        if not stdout:
            raise DesktopWorkerError(f"desktop adapter returned no result for {action}")
        try:
            result = json.loads(stdout.splitlines()[-1])
        except json.JSONDecodeError as exc:
            raise DesktopWorkerError(
                f"desktop adapter returned invalid JSON for {action}: {stdout[-500:]}"
            ) from exc
        if not isinstance(result, dict):
            raise DesktopWorkerError(f"desktop adapter returned a non-object for {action}")
        if not result.get("ok", False):
            detail = str(result.get("error") or f"desktop action failed: {action}")
            stage = str(result.get("stage") or "").strip()
            category = str(result.get("category") or "").strip()
            LOGGER.error(
                "Desktop action rejected action=%s stage=%s category=%s detail=%s stack=%s",
                action,
                stage or "unknown",
                category or "unknown",
                detail,
                str(result.get("script_stack_trace") or "").strip(),
            )
            prefix = f"[stage={stage}] " if stage else ""
            raise DesktopWorkerError(prefix + detail)
        LOGGER.info("Desktop action completed action=%s", action)
        return result

    def list_windows(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return self.execute(
            "list_windows",
            arguments,
            timeout_seconds=min(self._timeout_seconds, 12),
        )

    def preflight(self) -> dict[str, Any]:
        """Probe UI Automation readiness without consuming an action timeout."""

        return self.execute(
            "list_windows",
            {},
            timeout_seconds=self._preflight_timeout_seconds,
        )

    def inspect_window(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return self.execute(
            "inspect_window",
            arguments,
            timeout_seconds=min(self._timeout_seconds, 30),
        )

    def set_value(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return self.execute(
            "set_value",
            arguments,
            timeout_seconds=min(self._timeout_seconds, 20),
        )

    def invoke(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return self.execute(
            "invoke",
            arguments,
            timeout_seconds=min(self._timeout_seconds, 20),
        )

    def capture(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        request = dict(arguments)
        request.setdefault("screenshot_directory", str(self._screenshot_directory))
        return self.execute(
            "capture",
            request,
            timeout_seconds=min(self._timeout_seconds, 20),
        )

    def ask_doubao(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        request = dict(arguments)
        question = str(request.get("question") or "").strip()
        if not question:
            raise DesktopWorkerError("question cannot be empty")
        request["question"] = question
        request.setdefault("process_name", "Doubao")
        request["window_timeout_seconds"] = _bounded_number(
            request.get(
                "window_timeout_seconds",
                DEFAULT_DOUBAO_WINDOW_TIMEOUT_SECONDS,
            ),
            name="window_timeout_seconds",
            minimum=3.0,
            maximum=MAX_DOUBAO_WINDOW_TIMEOUT_SECONDS,
        )
        request["answer_timeout_seconds"] = _bounded_number(
            request.get(
                "answer_timeout_seconds",
                DEFAULT_DOUBAO_ANSWER_TIMEOUT_SECONDS,
            ),
            name="answer_timeout_seconds",
            minimum=5.0,
            maximum=MAX_DOUBAO_ANSWER_TIMEOUT_SECONDS,
        )
        request["stable_seconds"] = _bounded_number(
            request.get("stable_seconds", DEFAULT_DOUBAO_STABLE_SECONDS),
            name="stable_seconds",
            minimum=2.0,
            maximum=MAX_DOUBAO_STABLE_SECONDS,
        )
        request.setdefault("screenshot_directory", str(self._screenshot_directory))
        if self._doubao_launch_path:
            request.setdefault("launch_path", str(self._doubao_launch_path))
        requested_budget = (
            float(request["window_timeout_seconds"])
            + float(request["answer_timeout_seconds"])
            + DOUBAO_COORDINATION_GRACE_SECONDS
        )
        if requested_budget > self._timeout_seconds:
            raise DesktopWorkerError(
                "doubao_ask timeout budget exceeds the desktop action deadline: "
                f"requested {requested_budget:g}s, available "
                f"{self._timeout_seconds:g}s"
            )
        return self.execute(
            "doubao_ask",
            request,
            timeout_seconds=self._timeout_seconds,
        )


def _positive_finite_number(value: Any, *, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite positive number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return parsed


def _bounded_number(
    value: Any,
    *,
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise DesktopWorkerError(
            f"{name} must be between {minimum:g} and {maximum:g} seconds"
        ) from exc
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        raise DesktopWorkerError(
            f"{name} must be between {minimum:g} and {maximum:g} seconds"
        )
    return parsed
