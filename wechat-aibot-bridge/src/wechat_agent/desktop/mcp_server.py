"""Small stdio MCP server exposing deterministic Windows desktop tools."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from .worker import (
    DEFAULT_ACTION_TIMEOUT_SECONDS,
    DEFAULT_DOUBAO_ANSWER_TIMEOUT_SECONDS,
    DEFAULT_DOUBAO_STABLE_SECONDS,
    DEFAULT_DOUBAO_WINDOW_TIMEOUT_SECONDS,
    DEFAULT_PREFLIGHT_TIMEOUT_SECONDS,
    MAX_DOUBAO_ANSWER_TIMEOUT_SECONDS,
    MAX_DOUBAO_STABLE_SECONDS,
    MAX_DOUBAO_WINDOW_TIMEOUT_SECONDS,
    DesktopWorker,
    DesktopWorkerError,
)


LOGGER = logging.getLogger(__name__)
SERVER_NAME = "wecom-windows-desktop"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"


WINDOW_PROPERTIES = {
    "process_name": {"type": "string", "description": "Executable process name, for example Doubao."},
    "title_contains": {"type": "string", "description": "Case-insensitive window-title fragment."},
}
CONTROL_PROPERTIES = {
    "control_type": {"type": "string", "description": "UIA control type such as Edit, Button, Document, Text or Pane."},
    "name_contains": {"type": "string", "description": "Case-insensitive accessible-name fragment."},
    "automation_id": {"type": "string", "description": "Exact UI Automation AutomationId."},
    "class_name": {"type": "string", "description": "Exact UI Automation class name."},
    "index": {"type": "integer", "minimum": 0, "description": "Zero-based match index after filtering."},
}


def _schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "list_windows",
        "description": "List real top-level Windows UI Automation windows. Use this instead of treating a running process as proof that an app is interactive.",
        "inputSchema": _schema(WINDOW_PROPERTIES),
    },
    {
        "name": "inspect_window",
        "description": "Inspect accessible controls in one top-level window, including control type, name, AutomationId, supported patterns and current value.",
        "inputSchema": _schema({
            **WINDOW_PROPERTIES,
            "max_controls": {"type": "integer", "minimum": 1, "maximum": 500, "default": 120},
        }),
    },
    {
        "name": "set_value",
        "description": "Set and verify the value of a specific UI Automation control through ValuePattern. It never sends global keystrokes.",
        "inputSchema": _schema({**WINDOW_PROPERTIES, **CONTROL_PROPERTIES, "value": {"type": "string"}}, ("value",)),
    },
    {
        "name": "invoke",
        "description": "Invoke a specific button or actionable UI Automation control and report the exact matched element.",
        "inputSchema": _schema({**WINDOW_PROPERTIES, **CONTROL_PROPERTIES}),
    },
    {
        "name": "capture",
        "description": "Capture the bounds of one visible target window to a PNG file and return its absolute path.",
        "inputSchema": _schema({
            **WINDOW_PROPERTIES,
            "file_name": {"type": "string", "description": "Optional safe PNG file name."},
        }),
    },
    {
        "name": "doubao_ask",
        "description": "Complete the verified Doubao transaction: launch/show Doubao, find its real UIA input control, set and verify the question, invoke Send, wait for accessible answer text to change and stabilize, then capture the Doubao window. Return screenshot_path for WeCom delivery.",
        "inputSchema": _schema({
            "question": {"type": "string", "minLength": 1},
            "window_timeout_seconds": {
                "type": "number",
                "minimum": 3,
                "maximum": MAX_DOUBAO_WINDOW_TIMEOUT_SECONDS,
                "default": DEFAULT_DOUBAO_WINDOW_TIMEOUT_SECONDS,
                "description": "Window discovery budget; bounded by the desktop action deadline.",
            },
            "answer_timeout_seconds": {
                "type": "number",
                "minimum": 5,
                "maximum": MAX_DOUBAO_ANSWER_TIMEOUT_SECONDS,
                "default": DEFAULT_DOUBAO_ANSWER_TIMEOUT_SECONDS,
                "description": "Answer wait budget; it cannot exceed the upstream MCP/action deadline.",
            },
            "stable_seconds": {
                "type": "number",
                "minimum": 2,
                "maximum": MAX_DOUBAO_STABLE_SECONDS,
                "default": DEFAULT_DOUBAO_STABLE_SECONDS,
            },
        }, ("question",)),
    },
)


def _configure_logging() -> None:
    level_name = os.getenv("DSH_DESKTOP_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_file = os.getenv("DSH_DESKTOP_LOG_FILE", "").strip()
    if log_file:
        path = Path(log_file).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def _worker_from_environment() -> DesktopWorker:
    powershell = os.getenv("DSH_DESKTOP_POWERSHELL", "").strip()
    if not powershell:
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"
    script_path = Path(os.environ["DSH_DESKTOP_UIA_SCRIPT"])
    screenshot_directory = Path(
        os.getenv("DSH_DESKTOP_SCREENSHOT_DIR", "").strip()
        or (Path.home() / "Pictures" / "WeComAgent")
    )
    launch_raw = os.getenv("DSH_DOUBAO_LAUNCH_PATH", "").strip()
    return DesktopWorker(
        powershell_bin=powershell,
        script_path=script_path,
        timeout_seconds=float(os.getenv(
            "DSH_DESKTOP_ACTION_TIMEOUT_SECONDS",
            str(DEFAULT_ACTION_TIMEOUT_SECONDS),
        )),
        preflight_timeout_seconds=float(os.getenv(
            "DSH_DESKTOP_PREFLIGHT_TIMEOUT_SECONDS",
            str(DEFAULT_PREFLIGHT_TIMEOUT_SECONDS),
        )),
        screenshot_directory=screenshot_directory,
        doubao_launch_path=Path(launch_raw) if launch_raw else None,
    )


def _tool_handlers(worker: DesktopWorker) -> dict[str, Any]:
    return {
        "list_windows": worker.list_windows,
        "inspect_window": worker.inspect_window,
        "set_value": worker.set_value,
        "invoke": worker.invoke,
        "capture": worker.capture,
        "doubao_ask": worker.ask_doubao,
    }


def _success(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _failure(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def handle_request(
    message: dict[str, Any],
    worker: DesktopWorker,
) -> dict[str, Any] | None:
    """Handle the MCP subset used by the Harness stdio client."""

    method = message.get("method")
    request_id = message.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        params = message.get("params") or {}
        return _success(request_id, {
            "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "ping":
        return _success(request_id, {})
    if method == "tools/list":
        LOGGER.info("MCP tool catalog requested count=%s", len(TOOLS))
        return _success(request_id, {"tools": list(TOOLS)})
    if method == "tools/call":
        params = message.get("params") or {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        handler = _tool_handlers(worker).get(name)
        if handler is None:
            return _failure(request_id, -32602, f"unknown desktop tool: {name}")
        if not isinstance(arguments, dict):
            return _failure(request_id, -32602, "tool arguments must be an object")
        LOGGER.info("MCP tool call started tool=%s", name)
        try:
            result = handler(arguments)
        except DesktopWorkerError as exc:
            LOGGER.warning("MCP tool call failed tool=%s error=%s", name, exc)
            return _success(request_id, {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            })
        except Exception as exc:
            LOGGER.exception("Unexpected desktop tool failure tool=%s", name)
            return _success(request_id, {
                "content": [{"type": "text", "text": f"unexpected desktop worker error: {exc}"}],
                "isError": True,
            })
        LOGGER.info("MCP tool call completed tool=%s", name)
        return _success(request_id, {
            "content": [{
                "type": "text",
                "text": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            }],
            "structuredContent": result,
        })
    if request_id is None:
        return None
    return _failure(request_id, -32601, f"method not found: {method}")


def main() -> None:
    """Serve newline-delimited JSON-RPC on stdio; stdout is protocol-only."""

    _configure_logging()
    try:
        worker = _worker_from_environment()
    except Exception:
        LOGGER.exception("Desktop MCP server configuration failed")
        raise SystemExit(2)
    try:
        preflight = worker.preflight()
    except Exception:
        LOGGER.exception("Desktop MCP window-enumeration preflight failed")
        raise SystemExit(2)
    LOGGER.info(
        "Desktop MCP server started preflight_windows=%s",
        preflight.get("count", 0),
    )
    for raw_line in sys.stdin.buffer:
        try:
            message = json.loads(raw_line.decode("utf-8"))
            if not isinstance(message, dict):
                raise ValueError("request must be a JSON object")
            response = handle_request(message, worker)
        except Exception as exc:
            LOGGER.exception("Invalid MCP request")
            response = _failure(None, -32700, str(exc))
        if response is not None:
            payload = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
            sys.stdout.buffer.write(payload.encode("utf-8") + b"\n")
            sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
