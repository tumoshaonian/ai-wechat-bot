"""DeepSeek Harness computer-operation backend."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

from ..domain import AgentReply, AgentTaskInterrupted, IncomingMessage, UserVisibleError
from ..ports import ChatBackend
from ..session_registry import HarnessConversationStatus, HarnessSessionLease, HarnessSessionRegistry
from ..telemetry import NULL_EVENT_RECORDER, EventRecorder, safe_record

if TYPE_CHECKING:
    from ..config import Settings


LOGGER = logging.getLogger(__name__)
FILE_TAG_PATTERN = re.compile(r"<wechat-file>\s*(.*?)\s*</wechat-file>", re.IGNORECASE | re.DOTALL)
SAFE_ERROR_CODE_PATTERN = re.compile(r"^[A-Z0-9_-]{1,64}$")
API_KEY_DETAIL_PATTERN = re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;]+")
BEARER_DETAIL_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
DESKTOP_FILE_TOOLS = frozenset({
    "mcp__desktop__capture",
    "mcp__desktop__doubao_ask",
})


class HarnessLike(Protocol):
    """The small synchronous SDK surface used by this adapter."""

    def start(self) -> None:
        """Start and initialize the owned runtime."""

    def run(self, input: str, *, session_id: str) -> Any:
        """Run one agent turn."""

    def close(self) -> None:
        """Stop the owned runtime process."""


HarnessFactory = Callable[["Settings"], HarnessLike]


class DeepSeekHarnessBackend(ChatBackend):
    """Reuse one local Harness runtime and preserve one session per chat."""

    def __init__(
        self,
        settings: Settings,
        *,
        harness_factory: HarnessFactory | None = None,
        event_recorder: EventRecorder = NULL_EVENT_RECORDER,
    ) -> None:
        self._settings = settings
        self._harness_factory = harness_factory or _create_harness
        self._harness: HarnessLike | None = None
        self._operation_lock = asyncio.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dsh-runtime")
        self._runtime_guard = threading.RLock()
        self._registry = HarnessSessionRegistry(settings.harness_session_root)
        self._event_recorder = event_recorder
        self._active_chat_session_id: str | None = None
        self._active_task_id: str | None = None
        self._tool_names: dict[str, str] = {}
        self._interrupt_requested_for: set[str] = set()
        self._closed = False

    async def start(self) -> None:
        """Fail fast before WeCom connects if the SDK profile cannot initialize."""

        if self._closed:
            raise RuntimeError("DeepSeek Harness backend is closed")
        async with self._operation_lock:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(self._executor, self._start_sync)
            except Exception:
                await asyncio.to_thread(self._close_runtime_sync)
                raise

    def _start_sync(self) -> None:
        with self._runtime_guard:
            if self._harness is None:
                self._harness = self._harness_factory(self._settings)
            harness = self._harness
        start = getattr(harness, "start", None)
        if callable(start):
            start()

    async def reply(self, message: IncomingMessage) -> AgentReply:
        if self._closed:
            raise RuntimeError("DeepSeek Harness backend is closed")

        async with self._operation_lock:
            lease = self._registry.begin(message.session_id)
            if lease.recovered_interrupted_session:
                LOGGER.warning(
                    "Rotated an unclean Harness session before processing chat_session=%s generation=%s",
                    message.session_id,
                    lease.generation,
                )
            with self._runtime_guard:
                self._active_chat_session_id = message.session_id
                self._active_task_id = message.task_id
            self._record_agent_event(
                message,
                "agent.session.started",
                {
                    "harness_session_id": lease.session_id,
                    "generation": lease.generation,
                    "recovered_interrupted_session": lease.recovered_interrupted_session,
                },
            )
            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    self._executor,
                    self._run_sync,
                    message,
                    lease.session_id,
                )
            except asyncio.CancelledError:
                # Cancelling an asyncio future does not stop the synchronous
                # SDK call already running in the executor. The SDK currently
                # has no per-turn cancel request, so terminate the owned Runtime
                # before allowing the cancellation to escape.
                already_interrupted = self._consume_interrupt(message.session_id)
                await asyncio.to_thread(self._close_runtime_sync)
                if not already_interrupted:
                    self._registry.rotate(message.session_id, reason="request-cancelled")
                raise
            except Exception as exc:
                error_code, user_message = _friendly_harness_exception(
                    exc,
                    phase="request",
                )
                safe_detail = _redact_harness_detail(str(exc))
                self._record_agent_event(
                    message,
                    "agent.session.failed",
                    {
                        "harness_session_id": lease.session_id,
                        "generation": lease.generation,
                        "error_code": error_code,
                        "error": safe_detail,
                    },
                    severity="ERROR",
                )
                if self._consume_interrupt(message.session_id):
                    raise AgentTaskInterrupted("DeepSeek Harness task was stopped") from exc
                # The SDK has no per-turn cancel method. A transport/protocol/
                # timeout exception may leave work running in the child, so a
                # failed Runtime is never reused for the next WeCom request.
                await asyncio.to_thread(self._close_runtime_sync)
                self._registry.rotate(message.session_id, reason="runtime-error")
                raise UserVisibleError(user_message, code=error_code) from exc
            finally:
                with self._runtime_guard:
                    if self._active_chat_session_id == message.session_id:
                        self._active_chat_session_id = None
                        self._active_task_id = None
                self._registry.finish(lease)

        finish_reason = getattr(result, "finish_reason", None)
        if finish_reason == "error":
            detail, code = _harness_error_detail(result)
            safe_code = _normalize_error_code(code)
            safe_detail = _redact_harness_detail(detail)
            suffix = f" [{safe_code}]" if safe_code else ""
            LOGGER.error("DeepSeek Harness finished with an error%s: %s", suffix, safe_detail)
            await asyncio.to_thread(self._close_runtime_sync)
            self._registry.rotate(message.session_id, reason=f"agent-error:{safe_code}")
            self._record_agent_event(
                message,
                "agent.session.failed",
                {
                    "harness_session_id": lease.session_id,
                    "generation": lease.generation,
                    "error_code": safe_code,
                    "error": safe_detail,
                },
                severity="ERROR",
            )
            raise UserVisibleError(
                _friendly_harness_error(detail, safe_code),
                code=safe_code,
            )
        if finish_reason not in {"completed", "max-tokens"}:
            await asyncio.to_thread(self._close_runtime_sync)
            self._registry.rotate(
                message.session_id,
                reason=f"protocol-finish:{finish_reason or 'missing'}",
            )
            raise UserVisibleError(
                "Agent 返回了不完整的结束状态，Runtime 已重建以避免后续任务受影响，请重试。",
                code="AGENT_PROTOCOL_ERROR",
            )
        final_response = str(getattr(result, "final_response", "") or "").strip()
        if not final_response:
            await asyncio.to_thread(self._close_runtime_sync)
            self._registry.rotate(message.session_id, reason="empty-response")
            raise UserVisibleError(
                "Agent 没有生成可发送的结果，Runtime 已重建，请重试。",
                code="AGENT_EMPTY_RESPONSE",
            )
        reply = _extract_file_deliveries(final_response)
        if finish_reason == "max-tokens":
            warning = "⚠️ Agent 已达到本轮输出上限，以下结果可能不完整；可继续追问让它完成剩余步骤。"
            reply = AgentReply(
                "\n\n".join(part for part in (reply.text, warning) if part),
                reply.files,
            )
        event_files = _extract_desktop_tool_deliveries(
            getattr(result, "events", None),
            notifications=getattr(result, "notifications", None),
            root_session_id=lease.session_id,
        )
        if event_files:
            merged_files = tuple(dict.fromkeys((*reply.files, *event_files)))
            reply = AgentReply(reply.text, merged_files)
        LOGGER.info(
            "DeepSeek Harness completed session=%s finish_reason=%s files=%s",
            message.session_id,
            finish_reason,
            len(reply.files),
        )
        self._record_agent_event(
            message,
            "agent.session.completed",
            {
                "harness_session_id": lease.session_id,
                "generation": lease.generation,
                "finish_reason": finish_reason,
                "file_count": len(reply.files),
                "result": reply.text,
            },
        )
        return reply

    def _run_sync(self, message: IncomingMessage, session_id: str) -> Any:
        with self._runtime_guard:
            if self._harness is None:
                self._harness = self._harness_factory(self._settings)
            harness = self._harness
        callback = lambda notification: self._record_notification(  # noqa: E731
            message,
            session_id,
            notification,
        )
        try:
            parameters = inspect.signature(harness.run).parameters.values()
            supports_notifications = any(
                parameter.name == "on_notification"
                or parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            supports_notifications = False
        if supports_notifications:
            return harness.run(
                message.content,
                session_id=session_id,
                on_notification=callback,
            )
        return harness.run(message.content, session_id=session_id)

    def _record_notification(
        self,
        message: IncomingMessage,
        session_id: str,
        notification: Any,
    ) -> None:
        method = str(getattr(notification, "method", "") or "unknown")
        raw_payload = getattr(notification, "payload", None)
        payload = dict(raw_payload) if isinstance(raw_payload, dict) else {}
        event = payload.get("event") if method == "session.event" else None
        harness_type = str(event.get("type") or "") if isinstance(event, dict) else ""
        if harness_type == "tool/call":
            event_type = "tool.started"
        elif harness_type == "tool/result":
            event_type = (
                "tool.failed" if _tool_result_is_error(event) else "tool.completed"
            )
        else:
            event_type = "agent.notification"
        tool_call_id = _tool_call_id(event) if isinstance(event, dict) else None
        event_data = event.get("data") if isinstance(event, dict) else None
        event_data = event_data if isinstance(event_data, dict) else {}
        direct_name = event_data.get("name")
        if tool_call_id and isinstance(direct_name, str) and direct_name:
            with self._runtime_guard:
                self._tool_names[tool_call_id] = direct_name
        with self._runtime_guard:
            tool_name = self._tool_names.get(tool_call_id or "")
            if harness_type == "tool/result" and tool_call_id:
                self._tool_names.pop(tool_call_id, None)
        safe_record(
            self._event_recorder,
            event_type,
            trace_id=message.trace_id,
            actor_type="agent",
            actor_id=message.connection_id,
            resource_type="tool_call" if tool_call_id else "task",
            resource_id=tool_call_id or message.task_id,
            payload={
                "connection_id": message.connection_id,
                "task_id": message.task_id,
                "message_id": message.message_id,
                "session_id": message.session_id,
                "harness_session_id": session_id,
                "notification_method": method,
                "harness_event_type": harness_type or None,
                "tool_call_id": tool_call_id,
                "tool_name": tool_name or direct_name or "unknown",
                "input": (
                    event_data.get("input")
                    or event_data.get("arguments")
                    or event_data.get("args")
                    or {}
                ),
                "output": event_data if harness_type == "tool/result" else {},
                "notification": payload,
            },
            severity="ERROR" if event_type == "tool.failed" else "INFO",
        )

    def _record_agent_event(
        self,
        message: IncomingMessage,
        event_type: str,
        payload: dict[str, object],
        *,
        severity: str = "INFO",
    ) -> None:
        safe_record(
            self._event_recorder,
            event_type,
            trace_id=message.trace_id,
            actor_type="agent",
            actor_id=message.connection_id,
            resource_type="task",
            resource_id=message.task_id,
            payload={
                "connection_id": message.connection_id,
                "task_id": message.task_id,
                "message_id": message.message_id,
                "session_id": message.session_id,
                **payload,
            },
            severity=severity,
        )

    def is_busy(self, chat_session_id: str) -> bool:
        with self._runtime_guard:
            return self._active_chat_session_id == chat_session_id

    def session_status(self, chat_session_id: str) -> HarnessConversationStatus:
        return self._registry.status(chat_session_id)

    async def stop_session(
        self,
        chat_session_id: str,
    ) -> tuple[bool, HarnessConversationStatus]:
        interrupted = await self.interrupt_session(chat_session_id)
        status = self._registry.rotate(chat_session_id, reason="stopped") if interrupted else self._registry.status(chat_session_id)
        return interrupted, status

    async def stop_task(
        self,
        task_id: str,
    ) -> tuple[bool, HarnessConversationStatus | None]:
        """Stop the exact active task without cancelling a newer task by accident."""

        with self._runtime_guard:
            if self._active_task_id != task_id:
                return False, None
            chat_session_id = self._active_chat_session_id
        if chat_session_id is None:
            return False, None
        return await self.stop_session(chat_session_id)

    async def end_session(
        self,
        chat_session_id: str,
    ) -> tuple[bool, HarnessConversationStatus]:
        interrupted = await self.interrupt_session(chat_session_id)
        return interrupted, self._registry.rotate(chat_session_id, reason="ended")

    async def interrupt_session(self, chat_session_id: str) -> bool:
        with self._runtime_guard:
            if self._active_chat_session_id != chat_session_id:
                return False
            self._interrupt_requested_for.add(chat_session_id)
        await asyncio.to_thread(self._close_runtime_sync)
        return True

    def _consume_interrupt(self, chat_session_id: str) -> bool:
        with self._runtime_guard:
            if chat_session_id not in self._interrupt_requested_for:
                return False
            self._interrupt_requested_for.discard(chat_session_id)
            return True

    def _close_runtime_sync(self) -> None:
        with self._runtime_guard:
            harness = self._harness
            self._harness = None
        if harness is not None:
            harness.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._close_runtime_sync)
        await asyncio.to_thread(self._executor.shutdown, True, cancel_futures=True)


def _harness_error_detail(result: Any) -> tuple[str, str | None]:
    events = getattr(result, "events", None)
    if isinstance(events, list):
        for event in reversed(events):
            if not isinstance(event, dict) or event.get("type") != "turn/end":
                continue
            data = event.get("data")
            reason = data.get("reason") if isinstance(data, dict) else None
            if not isinstance(reason, dict):
                continue
            failure = reason.get("error") or reason.get("failure")
            if isinstance(failure, dict):
                message = str(failure.get("message") or "unknown Harness error")
                code = failure.get("code")
                normalized_code = str(code) if code else None
                if _is_session_collision(message):
                    normalized_code = "SESSION_COLLISION"
                return message, normalized_code
    return "unknown Harness error", None


def _friendly_harness_error(detail: str, code: str | None) -> str:
    normalized = _normalize_error_code(code)
    if normalized == "SESSION_COLLISION" or _is_session_collision(detail):
        return (
            "检测到旧会话记录与当前 Harness Runtime 的会话编号冲突。"
            "本次尚未执行电脑操作，Bridge 已切换到一个全新的会话；"
            "请直接重新发送刚才的任务。若重试后仍出现，请重启桌面启动器。"
        )
    if normalized == "QUOTA":
        return (
            "DeepSeek API 余额不足，Agent 暂时无法继续执行。请为当前 API Key 充值，"
            "或在电脑端更换有余额的模型/API Key 后重试；本次失败会话已自动隔离。"
        )
    if normalized == "MISSING_CREDENTIAL":
        return (
            "电脑端未配置 DeepSeek API Key，Agent 无法调用模型。"
            "请配置 DEEPSEEK_API_KEY 后重启机器人。"
        )
    if normalized == "INVALID_CREDENTIAL":
        return (
            "电脑端 DeepSeek API Key 格式无效。请重新粘贴完整 Key（不要包含引号、"
            "空格或 Bearer 前缀）后重启。"
        )
    if normalized in {"AUTH", "AUTHENTICATION", "UNAUTHORIZED"}:
        return (
            "DeepSeek API Key 被服务端拒绝或没有权限，请检查 Key、接口地址和账号权限后重试。"
        )
    if normalized == "NO_ADAPTER":
        return (
            "当前 Harness 配置没有加载所选模型提供商，请检查 HARNESS_PROFILE 和 "
            "HARNESS_PROVIDER 后重启。"
        )
    if normalized == "UNKNOWN_MODEL":
        return "当前提供商不支持配置的模型，请检查 HARNESS_MODEL 后重启。"
    if normalized == "CONTEXT_WINDOW_EXCEEDED":
        return (
            "当前会话内容超过模型上下文上限，已切换到新会话；"
            "请缩短任务或减少附件后重新发送。"
        )
    if normalized == "RATE_LIMIT":
        return "DeepSeek API 当前触发限流，请稍后再试；本次失败会话已自动隔离。"
    if normalized == "TIMEOUT":
        return "模型服务响应超时，Harness 多次尝试后仍未恢复；请稍后重试。"
    if normalized == "TRANSPORT":
        return "与模型服务的网络连接中断，Harness 多次尝试后仍未恢复；请检查网络后重试。"
    if normalized == "SERVER":
        return "模型服务暂时异常，请稍后重试。"
    if normalized == "EMPTY_RESPONSE":
        return "模型没有返回有效内容，请重新发送任务。"
    if normalized == "INVALID_REQUEST":
        return "模型拒绝了本次请求，请缩短内容、减少附件或新建会话后重试。"
    if normalized == "TOOL_TIMEOUT" or normalized.endswith("_TIMEOUT"):
        return (
            "电脑操作工具执行超时，本次失败会话已隔离，"
            "请确认目标软件状态后重试。"
        )
    return (
        f"Agent 执行失败（{normalized}）。本次失败会话已自动隔离，请重试；"
        "若持续失败请查看电脑端日志。"
    )


def _friendly_harness_exception(
    error: Exception,
    *,
    phase: str,
) -> tuple[str, str]:
    """Map SDK/runtime failures without exposing local diagnostics to WeCom."""

    if isinstance(error, TimeoutError):
        code = (
            "HARNESS_INITIALIZE_TIMEOUT"
            if phase == "initialize"
            else "HARNESS_REQUEST_TIMEOUT"
        )
        return code, "本地 Harness 等待超时，Runtime 已重建，请重试。"

    class_name = type(error).__name__
    if class_name == "TransportClosedError":
        return (
            "HARNESS_TRANSPORT_CLOSED",
            "本地 Harness Runtime 意外退出，已自动重建；请重试，若仍失败请查看电脑端日志。",
        )
    if class_name == "SdkProtocolError":
        return (
            "HARNESS_PROTOCOL_ERROR",
            "Harness 返回了无法识别的协议数据，Runtime 已重建；请查看电脑端日志。",
        )
    if class_name == "JsonRpcError":
        return (
            "HARNESS_RPC_ERROR",
            "Harness 初始化或调用失败，请检查 Profile、模型与桌面工具配置。",
        )
    if isinstance(error, (FileNotFoundError, OSError)):
        return (
            "HARNESS_RUNTIME_UNAVAILABLE",
            "未找到或无法启动 Harness Runtime，请在电脑端重新构建或配置 Runtime。",
        )
    return (
        "HARNESS_RUNTIME_ERROR",
        "本地 Harness 执行异常，Runtime 已重建；请重试，若仍失败请查看电脑端日志。",
    )


def _normalize_error_code(code: str | None) -> str:
    normalized = str(code or "UNKNOWN").strip().upper()
    return normalized if SAFE_ERROR_CODE_PATTERN.fullmatch(normalized) else "UNKNOWN"


def _redact_harness_detail(detail: str) -> str:
    """Remove credentials from diagnostics before logging or persistence."""

    redacted = str(detail)
    configured_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if configured_key:
        redacted = redacted.replace(configured_key, "[REDACTED]")
    redacted = API_KEY_DETAIL_PATTERN.sub(r"\1[REDACTED]", redacted)
    redacted = BEARER_DETAIL_PATTERN.sub("Bearer [REDACTED]", redacted)
    return redacted[:4000]


def _is_session_collision(detail: str) -> bool:
    normalized = detail.casefold()
    return "id collision" in normalized or (
        "persisted log" in normalized and "does not match" in normalized
    )


def _extract_file_deliveries(response: str) -> AgentReply:
    """Parse Harness-to-bridge file handoff tags and validate local paths."""

    paths: list[Path] = []
    errors: list[str] = []
    seen: set[Path] = set()
    for match in FILE_TAG_PATTERN.finditer(response):
        raw_path = os.path.expandvars(match.group(1).strip().strip('"'))
        try:
            path = Path(raw_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            errors.append(f"找不到待发送文件：{raw_path}")
            continue
        if not path.is_file():
            errors.append(f"待发送路径不是文件：{path}")
            continue
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)

    clean_text = FILE_TAG_PATTERN.sub("", response).strip()
    if errors:
        clean_text = "\n\n".join(part for part in (clean_text, *errors) if part)
    return AgentReply(clean_text, tuple(paths))


def _extract_desktop_tool_deliveries(
    events: Any,
    *,
    notifications: Any = None,
    root_session_id: str | None = None,
) -> tuple[Path, ...]:
    """Recover screenshots from trusted successful desktop-tool events.

    This is the deterministic fallback for cases where the model used the
    desktop tool correctly but forgot to repeat ``<wechat-file>`` in its final
    prose.  Only the two reviewed screenshot-producing MCP tools are accepted;
    arbitrary paths printed by shell or filesystem tools are ignored.  Current
    Harness SDK results intentionally keep ``events`` root-scoped, while
    ``notifications`` contains ``session.event`` envelopes for the complete
    sub-Agent tree.  Root events remain authoritative and only descendant
    events are added from the notification stream.
    """

    session_events = _result_session_events(
        events,
        notifications=notifications,
        root_session_id=root_session_id,
    )
    if not session_events:
        return ()

    desktop_calls: set[tuple[str | None, str]] = set()
    for session_id, event in session_events:
        if event.get("type") != "tool/call":
            continue
        data = event.get("data")
        if not isinstance(data, dict) or data.get("name") not in DESKTOP_FILE_TOOLS:
            continue
        call_id = data.get("callId")
        if isinstance(call_id, str) and call_id:
            desktop_calls.add((session_id, call_id))

    paths: list[Path] = []
    seen: set[Path] = set()
    for session_id, event in session_events:
        if event.get("type") != "tool/result":
            continue
        data = event.get("data")
        message = data.get("message") if isinstance(data, dict) else None
        source = message.get("source") if isinstance(message, dict) else None
        call_id = source.get("callId") if isinstance(source, dict) else None
        if (session_id, call_id) not in desktop_calls:
            continue
        for result_block in _tool_result_blocks(message):
            if result_block.get("isError") is True:
                continue
            for content_block in result_block.get("content", ()):
                if not isinstance(content_block, dict) or content_block.get("type") != "text":
                    continue
                payload = _json_object(content_block.get("text"))
                raw_path = payload.get("screenshot_path") if payload else None
                path = _existing_file(raw_path)
                if path is None or path in seen:
                    continue
                seen.add(path)
                paths.append(path)
    return tuple(paths)


def _result_session_events(
    events: Any,
    *,
    notifications: Any,
    root_session_id: str | None,
) -> tuple[tuple[str | None, dict[str, Any]], ...]:
    """Return root events plus descendant SDK ``session.event`` payloads.

    ``RunResult.notifications`` repeats root events as envelopes, so accepting
    those again would make the same event appear twice.  Descendant envelopes
    are the only source of child and grandchild activity because the SDK keeps
    ``RunResult.events`` root-scoped by design.
    """

    collected: list[tuple[str | None, dict[str, Any]]] = []
    if isinstance(events, list):
        collected.extend(
            (root_session_id, event)
            for event in events
            if isinstance(event, dict)
        )

    if not isinstance(notifications, list):
        return tuple(collected)

    for notification in notifications:
        method, payload = _notification_parts(notification)
        if method != "session.event":
            continue
        session_id = payload.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            continue
        if root_session_id is not None and session_id == root_session_id:
            continue
        event = payload.get("event")
        if isinstance(event, dict):
            collected.append((session_id, event))
    return tuple(collected)


def _notification_parts(notification: Any) -> tuple[str, dict[str, Any]]:
    """Normalize the Python SDK Notification and dictionary test doubles."""

    if isinstance(notification, dict):
        method = notification.get("method")
        raw_payload = notification.get("payload")
        if raw_payload is None:
            raw_payload = notification.get("params")
    else:
        method = getattr(notification, "method", None)
        raw_payload = getattr(notification, "payload", None)
    return (
        method if isinstance(method, str) else "",
        raw_payload if isinstance(raw_payload, dict) else {},
    )


def _tool_call_id(event: dict[str, Any]) -> str | None:
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    direct = data.get("callId")
    if isinstance(direct, str) and direct:
        return direct
    message = data.get("message")
    source = message.get("source") if isinstance(message, dict) else None
    nested = source.get("callId") if isinstance(source, dict) else None
    return nested if isinstance(nested, str) and nested else None


def _tool_result_is_error(event: Any) -> bool:
    """Return true when any nested MCP result block explicitly reports error."""

    if not isinstance(event, dict):
        return False
    data = event.get("data")
    message = data.get("message") if isinstance(data, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("isError") is True
        for block in content
    )


def _tool_result_blocks(message: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(message, dict):
        return ()
    content = message.get("content")
    if not isinstance(content, list):
        return ()
    return tuple(block for block in content if isinstance(block, dict))


def _json_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _existing_file(value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        path = Path(os.path.expandvars(value.strip().strip('"'))).expanduser().resolve(
            strict=True
        )
    except (OSError, RuntimeError):
        return None
    return path if path.is_file() else None


def _create_harness(settings: Settings) -> HarnessLike:
    try:
        from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig
    except ImportError as exc:
        raise RuntimeError(
            "deepseek-harness-sdk is not installed; follow the local SDK install command in README"
        ) from exc

    # The SDK runtime resolver currently reads DSH_RUNTIME_MODE from the parent
    # process (before it applies the child env mapping). This is a documented
    # dsh runtime selector and the Bridge owns exactly one Harness process.
    os.environ["DSH_RUNTIME_MODE"] = settings.harness_runtime_mode

    settings.harness_session_root.mkdir(parents=True, exist_ok=True)
    settings.harness_dsh_home.mkdir(parents=True, exist_ok=True)
    desktop_tool_timeout_seconds = min(
        settings.desktop_action_timeout_seconds + 30.0,
        max(1.0, settings.task_timeout_seconds - 30.0),
        max(1.0, settings.harness_request_timeout_seconds - 15.0),
    )
    runtime_env = {
        "DSH_SYSTEM_PROMPT": settings.harness_system_prompt,
        "DSH_PERMISSION_MODE": settings.harness_permission_mode,
        "DSH_RUNTIME_MODE": settings.harness_runtime_mode,
        "DSH_TELEMETRY_DISABLED": "1",
        "DSH_DESKTOP_ENABLED": "true" if settings.desktop_tools_enabled else "false",
        "DSH_DESKTOP_TOOL_TIMEOUT_MS": str(int(desktop_tool_timeout_seconds * 1000)),
    }
    if settings.desktop_tools_enabled:
        runtime_env.update(
            {
                "DSH_DESKTOP_PYTHON": sys.executable,
                "DSH_DESKTOP_PROJECT_ROOT": str(settings.desktop_uia_script.parents[2]),
                "DSH_DESKTOP_POWERSHELL": settings.desktop_powershell_bin,
                "DSH_DESKTOP_UIA_SCRIPT": str(settings.desktop_uia_script),
                "DSH_DESKTOP_ACTION_TIMEOUT_SECONDS": str(
                    settings.desktop_action_timeout_seconds
                ),
                "DSH_DESKTOP_SCREENSHOT_DIR": str(
                    settings.desktop_screenshot_directory
                ),
                "DSH_DESKTOP_LOG_FILE": str(settings.desktop_log_file),
                "DSH_DESKTOP_LOG_LEVEL": settings.log_level,
                "DSH_DOUBAO_LAUNCH_PATH": str(settings.doubao_launch_path or ""),
            }
        )
    config = DeepSeekHarnessConfig(
        provider=settings.harness_provider,
        model=settings.harness_model,
        reasoning_effort=settings.harness_reasoning_effort,
        max_tokens=settings.harness_max_tokens,
        cwd=str(settings.harness_workspace),
        runtime_cwd=str(settings.harness_workspace),
        dsh_bin=str(settings.harness_dsh_bin) if settings.harness_dsh_bin else None,
        profile=settings.harness_profile,
        patches=tuple(str(path) for path in settings.harness_patch_files),
        dsh_home=str(settings.harness_dsh_home),
        env=runtime_env,
        initialize_timeout_seconds=settings.harness_initialize_timeout_seconds,
        request_timeout_seconds=settings.harness_request_timeout_seconds,
        shutdown_timeout_seconds=settings.harness_shutdown_timeout_seconds,
    )
    return DeepSeekHarness(config)
