"""Transport-independent message processing use case."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from .domain import AgentReply, AgentTaskInterrupted, IncomingMessage, UserVisibleError
from .ports import ChatBackend, ConversationResponder
from .file_delivery import FileDeliverySession
from .telemetry import (
    NULL_EVENT_RECORDER,
    EventRecorder,
    safe_claim_message,
    safe_record,
)


LOGGER = logging.getLogger(__name__)


class _ResponseStreamUnavailable(RuntimeError):
    """The originating WeCom stream can no longer accept progress updates."""


class RecentMessageIds:
    """Bounded in-memory duplicate detector for retried transport messages."""

    def __init__(self, capacity: int = 2_000) -> None:
        self._capacity = max(1, capacity)
        self._ordered: deque[str] = deque()
        self._known: set[str] = set()

    def mark_if_new(self, message_id: str) -> bool:
        """Record a message ID and return false when it was already present."""

        if not message_id:
            return True
        if message_id in self._known:
            return False
        self._known.add(message_id)
        self._ordered.append(message_id)
        while len(self._ordered) > self._capacity:
            self._known.discard(self._ordered.popleft())
        return True


class _ObservedResponder:
    """Record outbound text and file deliveries around the real responder."""

    def __init__(
        self,
        delegate: ConversationResponder,
        recorder: EventRecorder,
        message: IncomingMessage,
    ) -> None:
        self._delegate = delegate
        self._recorder = recorder
        self._message = message
        self._outbound_sequence = 0

    async def send(self, text: str, *, finish: bool) -> None:
        self._outbound_sequence += 1
        payload = _message_payload(self._message) | {
            "content": text,
            "finish": finish,
            "direction": "outbound",
            "outbound_message_id": (
                f"{self._message.message_id or self._message.task_id}:"
                f"{self._outbound_sequence}"
            ),
        }
        try:
            await self._delegate.send(text, finish=finish)
        except Exception as exc:
            safe_record(
                self._recorder,
                "message.outbound.failed",
                trace_id=self._message.trace_id,
                actor_type="robot",
                actor_id=self._message.connection_id,
                resource_type="task",
                resource_id=self._message.task_id,
                payload=payload | {"error": str(exc)},
                severity="ERROR",
            )
            raise
        safe_record(
            self._recorder,
            "message.outbound",
            trace_id=self._message.trace_id,
            actor_type="robot",
            actor_id=self._message.connection_id,
            resource_type="task",
            resource_id=self._message.task_id,
            payload=payload,
        )

    async def send_file(self, path: Path) -> None:
        resolved = path.resolve()
        artifact_id = hashlib.sha256(
            f"{self._message.task_id}:{resolved}".encode("utf-8")
        ).hexdigest()
        digest = (
            await asyncio.to_thread(_sha256_file, resolved)
            if resolved.is_file()
            else None
        )
        payload = _message_payload(self._message) | {
            "artifact_id": artifact_id,
            "path": str(resolved),
            "name": resolved.name,
            "filename": resolved.name,
            "size_bytes": resolved.stat().st_size if resolved.is_file() else None,
            "sha256": digest,
        }
        safe_record(
            self._recorder,
            "artifact.created",
            trace_id=self._message.trace_id,
            actor_type="robot",
            actor_id=self._message.connection_id,
            resource_type="artifact",
            resource_id=artifact_id,
            payload=payload,
        )
        safe_record(
            self._recorder,
            "artifact.delivery.started",
            trace_id=self._message.trace_id,
            actor_type="robot",
            actor_id=self._message.connection_id,
            resource_type="task",
            resource_id=self._message.task_id,
            payload=payload,
        )
        try:
            await self._delegate.send_file(path)
        except Exception as exc:
            safe_record(
                self._recorder,
                "artifact.delivery.unknown",
                trace_id=self._message.trace_id,
                actor_type="robot",
                actor_id=self._message.connection_id,
                resource_type="task",
                resource_id=self._message.task_id,
                payload=payload | {"error": str(exc)},
                severity="ERROR",
            )
            raise
        safe_record(
            self._recorder,
            "artifact.delivery.succeeded",
            trace_id=self._message.trace_id,
            actor_type="robot",
            actor_id=self._message.connection_id,
            resource_type="task",
            resource_id=self._message.task_id,
            payload=payload,
        )


@dataclass
class _ManagedTask:
    message: IncomingMessage
    work: asyncio.Task
    settled: asyncio.Event
    stopping: bool = False


class MessageProcessor:
    """Authorize, serialize, execute, and report one inbound message."""

    def __init__(
        self,
        backend: ChatBackend,
        *,
        allowed_user_ids: frozenset[str] = frozenset(),
        progress_interval_seconds: float = 30,
        task_timeout_seconds: float = 480,
        event_recorder: EventRecorder = NULL_EVENT_RECORDER,
    ) -> None:
        self._backend = backend
        self._allowed_user_ids = allowed_user_ids
        self._recent_ids = RecentMessageIds()
        self._conversation_locks: dict[str, asyncio.Lock] = {}
        self._progress_interval_seconds = max(0.01, progress_interval_seconds)
        self._task_timeout_seconds = max(0.02, task_timeout_seconds)
        self._event_recorder = event_recorder
        self._tasks: dict[str, _ManagedTask] = {}
        self._ending_sessions: set[str] = set()
        self._closed = False
        bind_control = getattr(backend, "bind_task_controller", None)
        if callable(bind_control):
            bind_control(self)

    async def handle(
        self,
        message: IncomingMessage,
        responder: ConversationResponder,
    ) -> None:
        """Process one message and finish exactly one response stream."""

        if not self._recent_ids.mark_if_new(f"{message.connection_id}:{message.message_id}" if message.message_id else ""):
            LOGGER.info("Ignored duplicate message_id=%s", message.message_id)
            safe_record(
                self._event_recorder,
                "message.duplicate",
                actor_type="wecom_user",
                actor_id=message.sender_id,
                resource_type="message",
                resource_id=message.message_id,
                payload=_message_payload(message),
            )
            return
        if not safe_claim_message(
            self._event_recorder,
            message.connection_id,
            message.message_id,
        ):
            LOGGER.info("Ignored durable duplicate message_id=%s", message.message_id)
            safe_record(
                self._event_recorder,
                "message.duplicate",
                actor_type="wecom_user",
                actor_id=message.sender_id,
                resource_type="message",
                resource_id=message.message_id,
                payload=_message_payload(message) | {"source": "durable"},
            )
            return

        message = replace(
            message,
            task_id=message.task_id or uuid4().hex,
            trace_id=message.trace_id or uuid4().hex,
        )
        responder = _ObservedResponder(responder, self._event_recorder, message)
        safe_record(
            self._event_recorder,
            "message.received",
            trace_id=message.trace_id,
            actor_type="wecom_user",
            actor_id=message.sender_id,
            resource_type="message",
            resource_id=message.message_id,
            payload=_message_payload(message) | {"direction": "inbound"},
            idempotency_key=f"message:{message.connection_id}:{message.message_id}",
        )

        bootstrap_allowed = (
            not self._allowed_user_ids
            or message.sender_id in self._allowed_user_ids
        )
        authorized, authorization_reason = self._authorize_message(
            message,
            bootstrap_allowed=bootstrap_allowed,
        )
        if not authorized:
            LOGGER.warning("Rejected message from unauthorized user_id=%s", message.sender_id)
            safe_record(
                self._event_recorder,
                "message.rejected",
                trace_id=message.trace_id,
                actor_type="wecom_user",
                actor_id=message.sender_id,
                resource_type="message",
                resource_id=message.message_id,
                payload=_message_payload(message)
                | {"reason": authorization_reason},
                severity="WARNING",
            )
            await responder.send("当前账号没有使用此机器人的权限。", finish=True)
            return

        message = replace(
            message,
            access_policy=self._load_access_policy(message),
        )

        self._record_task(message, "task.queued", {"state": "queued"})

        control_handler = getattr(self._backend, "handle_control", None)
        if callable(control_handler):
            try:
                control_reply = await control_handler(message)
            except AgentTaskInterrupted:
                LOGGER.info("Agent task stopped for message_id=%s", message.message_id)
                self._record_task(message, "task.cancelled", {"state": "cancelled"})
                await responder.send("当前任务已停止。", finish=True)
                return
            except Exception:
                LOGGER.exception("Agent control command failed for message_id=%s", message.message_id)
                self._record_task(
                    message,
                    "task.failed",
                    {"state": "failed", "failure_stage": "control"},
                    severity="ERROR",
                )
                await responder.send("控制命令执行失败，请查看电脑端日志。", finish=True)
                return
            if control_reply is not None:
                try:
                    await responder.send(control_reply, finish=True)
                except Exception:
                    LOGGER.exception(
                        "Control reply delivery failed for message_id=%s",
                        message.message_id,
                    )
                    self._record_task(
                        message,
                        "task.failed",
                        {
                            "state": "failed",
                            "failure_stage": "response",
                            "error_code": "CONTROL_RESPONSE_SEND_FAILED",
                        },
                        severity="ERROR",
                    )
                    return
                self._record_task(
                    message,
                    "task.completed",
                    {"state": "succeeded", "kind": "control", "result": control_reply},
                )
                return

        if self._closed or message.session_id in self._ending_sessions:
            self._record_task(message, "task.cancelled", {"state": "cancelled", "reason": "session_closing"})
            await responder.send("当前会话正在结束或服务正在关闭，本条请求未执行，请稍后重新发送。", finish=True)
            return
        if message.task_id in self._tasks:
            raise RuntimeError("duplicate active task identity")
        entry = _ManagedTask(message, asyncio.create_task(self._handle_task(message, responder)), asyncio.Event())
        self._tasks[message.task_id] = entry
        try:
            await asyncio.shield(entry.work)
        except asyncio.CancelledError:
            if not entry.stopping:
                entry.stopping = True
                entry.work.cancel()
            # Do not declare cancelled while an owned executor is still draining.
            draining = asyncio.gather(entry.work, return_exceptions=True)
            while not draining.done():
                try:
                    await asyncio.shield(draining)
                except asyncio.CancelledError:
                    # Repeated caller cancellation must not release ownership early.
                    continue
            self._record_task(message, "task.cancelled", {
                "state": "cancelled", "reason": "processor_cancelled",
                "effects": "尚未执行的步骤已取消；已发生的操作不回滚，交付回执未知时不可盲目重试。",
            })
            try:
                await asyncio.wait_for(responder.send("当前任务已停止；已发生的操作不回滚，未确认的交付请先核实。", finish=True), 3)
            except Exception:
                LOGGER.warning("Could not deliver cancellation acknowledgement task_id=%s", message.task_id)
        finally:
            self._tasks.pop(message.task_id, None)
            entry.settled.set()

    async def _handle_task(self, message: IncomingMessage, responder: ConversationResponder) -> None:
        lock = self._conversation_locks.setdefault(message.session_id, asyncio.Lock())
        async with lock:
            self._record_task(message, "task.started", {"state": "running"})
            try:
                await responder.send("收到，正在处理…", finish=False)
            except Exception:
                LOGGER.exception(
                    "Initial response delivery failed for message_id=%s",
                    message.message_id,
                )
                self._record_task(
                    message,
                    "task.failed",
                    {
                        "state": "failed",
                        "failure_stage": "response",
                        "error_code": "INITIAL_RESPONSE_SEND_FAILED",
                    },
                    severity="ERROR",
                )
                return
            delivery = FileDeliverySession(message, responder, getattr(self._backend, "delivery_store", None))
            bind = getattr(self._backend, "bind_delivery", None)
            unbind = bind(message, delivery.send) if callable(bind) else lambda: None
            confirm_bind = getattr(self._backend, "bind_confirmation", None)
            confirm_unbind = confirm_bind(message, lambda text: responder.send(text, finish=False)) if callable(confirm_bind) else lambda: None
            try:
                try:
                    raw_reply = await self._reply_with_progress(message, responder)
                finally:
                    confirm_unbind()
                    unbind()
                reply = raw_reply if isinstance(raw_reply, AgentReply) else AgentReply(raw_reply)
                text = reply.text.strip()
                if not text and not reply.files:
                    raise RuntimeError("chat backend returned an empty reply")
                self._record_task(message, "agent.execution.completed", {
                    "execution_state": reply.status, "result": text,
                    "response_status": "not_attempted",
                    "meaning": "Agent 返回的执行结果，不等于文件交付或微信回复已确认。",
                })
            except AgentTaskInterrupted:
                LOGGER.info("Agent task stopped for message_id=%s", message.message_id)
                self._record_task(message, "task.cancelled", {"state": "cancelled"})
                await responder.send("当前任务已停止。", finish=True)
                return
            except UserVisibleError as exc:
                LOGGER.warning(
                    "User-visible backend failure for message_id=%s code=%s: %s",
                    message.message_id,
                    exc.code,
                    exc,
                )
                event_type = "task.timeout" if exc.code == "TASK_TIMEOUT" else "task.failed"
                self._record_task(
                    message,
                    event_type,
                    {
                        "state": "timed_out" if exc.code == "TASK_TIMEOUT" else "failed",
                        "error_code": exc.code,
                        "error": exc.user_message,
                    },
                    severity="ERROR",
                )
                await responder.send(exc.user_message, finish=True)
                return
            except _ResponseStreamUnavailable:
                LOGGER.warning(
                    "Response stream unavailable; aborted backend task for message_id=%s",
                    message.message_id,
                )
                self._record_task(
                    message,
                    "task.failed",
                    {"state": "failed", "error_code": "RESPONSE_STREAM_UNAVAILABLE"},
                    severity="ERROR",
                )
                return
            except Exception as exc:
                LOGGER.exception("Message processing failed for message_id=%s", message.message_id)
                self._record_task(
                    message,
                    "task.failed",
                    {
                        "state": "failed",
                        "failure_stage": "agent",
                        "error_code": "AGENT_RUNTIME_ERROR",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                    severity="ERROR",
                )
                await responder.send("任务处理失败，请查看电脑端日志后重试。", finish=True)
                return
            delivered = delivery.names(sent=True)
            failed = delivery.names(sent=False)
            if reply.files:
                try:
                    await responder.send(
                        f"已选中 {len(reply.files)} 个成果，正在发送…",
                        finish=False,
                    )
                except Exception:
                    LOGGER.exception(
                        "File-delivery notice failed for message_id=%s",
                        message.message_id,
                    )
                    self._record_delivery(message, {
                        "sent_files": delivered,
                        "not_attempted_files": [p.name for p in reply.files],
                        "reason": "发送提示失败，未尝试剩余文件交付。",
                    })
                    self._record_task(
                        message,
                        "task.completed",
                        {
                            "state": "partial_succeeded",
                            "failure_stage": "response",
                            "error_code": "FILE_NOTICE_SEND_FAILED",
                            "execution_state": reply.status,
                            "response_status": "unknown",
                            "result": text,
                            "error": "Agent 已返回结果，但文件发送提示未确认；剩余文件未尝试交付，不要自动重做执行步骤。",
                        },
                        severity="ERROR",
                    )
                    return
                for path in reply.files:
                    try:
                        if message.access_policy.get("can_send_files") is False or message.access_policy.get("can_read_files") is False:
                            raise PermissionError("当前账号没有读取并发送本地文件的权限")
                        receipt = await delivery.send(path)
                        if not receipt["ok"]:
                            raise RuntimeError(receipt.get("error", "交付未确认"))
                    except Exception:
                        failed.append(path.name)
                        LOGGER.exception(
                            "Failed to deliver file for message_id=%s path=%s",
                            message.message_id,
                            path,
                        )
                    else:
                        if path.name not in delivered:
                            delivered.append(path.name)

            # Persist actual channel outcomes before attempting the final text.
            self._record_delivery(message, {
                "sent_files": delivered, "failed_or_unknown_files": failed,
                "meaning": "sent 表示发送接口已确认，不代表用户已读；异常可能是回执丢失，禁止盲目重发。",
            })
            summary = text
            if delivered:
                summary = _append_line(summary, f"已发送文件：{', '.join(delivered)}")
            if failed:
                summary = "本次交付未全部确认，任务未全部完成。\n" + _append_line(summary, f"文件发送失败或结果未知：{', '.join(failed)}，请查看电脑端日志。")
            try:
                await responder.send(summary or "文件已发送。", finish=True)
            except Exception:
                LOGGER.exception(
                    "Final response delivery failed for message_id=%s",
                    message.message_id,
                )
                self._record_task(
                    message,
                    "task.completed",
                    {
                        "state": "partial_succeeded",
                        "failure_stage": "response",
                        "error_code": "FINAL_RESPONSE_SEND_FAILED",
                        "execution_state": reply.status,
                        "response_status": "unknown",
                        "result": text,
                        "error": "Agent 已返回结果，但最后一条微信回复未确认。请核对结果，不要自动重新执行任务。",
                        "delivered_files": delivered,
                        "failed_files": failed,
                    },
                    severity="ERROR",
                )
                return
            self._record_task(
                message,
                "task.completed",
                {
                    "state": "partial_succeeded" if failed else reply.status,
                    "execution_state": reply.status,
                    "response_status": "sent",
                    "result": text,
                    "delivered_files": delivered,
                    "failed_files": failed,
                },
                severity="WARNING" if failed else "INFO",
            )

    def _record_delivery(self, message: IncomingMessage, outcome: dict[str, object]) -> None:
        record = getattr(self._backend, "record_delivery", None)
        if callable(record):
            record(message, outcome)

    async def _reply_with_progress(
        self,
        message: IncomingMessage,
        responder: ConversationResponder,
    ) -> str | AgentReply:
        """Wait for a backend while periodically refreshing visible progress."""

        started_at = time.monotonic()
        task = asyncio.create_task(self._backend.reply(message))
        try:
            while not task.done():
                elapsed = time.monotonic() - started_at
                remaining = self._task_timeout_seconds - elapsed
                if remaining <= 0:
                    raise UserVisibleError(
                        f"任务已运行超过 {self._task_timeout_seconds:g} 秒，"
                        "为避免企业微信消息通道过期，已停止本次任务。请缩小任务范围后重试。",
                        code="TASK_TIMEOUT",
                    )
                done, _ = await asyncio.wait(
                    (task,),
                    timeout=min(self._progress_interval_seconds, remaining),
                )
                if done:
                    break
                elapsed = max(1, round(time.monotonic() - started_at))
                progress = getattr(self._backend, "progress", None)
                detail = progress(message.session_id) if callable(progress) else "仍在处理"
                try:
                    await responder.send(
                        f"{detail}，已用时约 {elapsed} 秒…",
                        finish=False,
                    )
                    waiting = getattr(self._backend, "waiting_confirmation", lambda _: False)(message.session_id)
                    self._record_task(
                        message,
                        "task.waiting" if waiting else "task.progress",
                        {"state": "waiting_confirmation" if waiting else "running", "elapsed_seconds": elapsed},
                    )
                except Exception as exc:
                    LOGGER.exception(
                        "Progress update failed; aborting backend task for message_id=%s",
                        message.message_id,
                    )
                    raise _ResponseStreamUnavailable from exc
            return await task
        finally:
            if not task.done():
                await self._abort_backend_session(message.session_id)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _abort_backend_session(self, chat_session_id: str) -> None:
        abort = getattr(self._backend, "abort_session", None)
        if not callable(abort):
            return
        try:
            await abort(chat_session_id)
        except Exception:
            LOGGER.exception(
                "Failed to abort backend after response stream/task timeout session=%s",
                chat_session_id,
            )

    async def close(self) -> None:
        """Release the configured backend."""
        if self._closed:
            return
        self._closed = True
        entries = list(self._tasks.values())
        self._request_stops(entries)
        await asyncio.gather(*(entry.settled.wait() for entry in entries))
        await self._backend.close()

    @staticmethod
    def _request_stops(entries: list[_ManagedTask]) -> int:
        count = 0
        for entry in entries:
            if not entry.work.done():
                count += 1
                if not entry.stopping:
                    entry.stopping = True
                    entry.work.cancel()
        return count

    async def cancel_task(self, task_id: str) -> dict[str, object]:
        """Cancel by identity, including waits outside the Harness runtime."""
        entry = self._tasks.get(task_id)
        if entry is None or entry.work.done():
            return {"interrupted": False, "state": "not_running"}
        self._request_stops([entry])
        await entry.settled.wait()
        return {"interrupted": True, "state": "cancelled", "task_id": task_id}

    async def stop_chat_session(self, session_id: str) -> dict[str, object]:
        entries = [entry for entry in self._tasks.values() if entry.message.session_id == session_id]
        count = self._request_stops(entries)
        await asyncio.gather(*(entry.settled.wait() for entry in entries))
        return {"interrupted": bool(count), "cancelled_tasks": count, "state": "stopped"}

    async def end_chat_session(self, session_id: str) -> dict[str, object]:
        if session_id in self._ending_sessions:
            raise RuntimeError("Session termination is already in progress")
        self._ending_sessions.add(session_id)
        try:
            stopped = await self.stop_chat_session(session_id)
            end = getattr(self._backend, "end_chat_session", None)
            if not callable(end):
                raise RuntimeError("The backend cannot end sessions")
            result = await end(session_id)
            return dict(result) | {"interrupted": stopped["interrupted"], "cancelled_tasks": stopped["cancelled_tasks"]}
        finally:
            self._ending_sessions.discard(session_id)

    def _record_task(
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
            payload=_message_payload(message) | payload,
            severity=severity,
        )

    def _authorize_message(
        self,
        message: IncomingMessage,
        *,
        bootstrap_allowed: bool,
    ) -> tuple[bool, str]:
        authorize = getattr(self._event_recorder, "authorize_wecom_user", None)
        if not callable(authorize):
            return bootstrap_allowed, "bootstrap_allowlist"
        try:
            decision = authorize(
                message.connection_id,
                message.sender_id,
                bootstrap_allowed=bootstrap_allowed,
            )
        except Exception:
            LOGGER.exception("User authorization lookup failed; using bootstrap allowlist")
            return bootstrap_allowed, "authorization_lookup_failed"
        if (
            isinstance(decision, tuple)
            and len(decision) == 2
            and isinstance(decision[0], bool)
        ):
            return decision[0], str(decision[1] or "policy")
        LOGGER.error("User authorization lookup returned an invalid decision")
        return bootstrap_allowed, "authorization_lookup_invalid"

    def _load_access_policy(self, message: IncomingMessage) -> dict[str, object]:
        loader = getattr(self._event_recorder, "get_wecom_user_policy", None)
        if not callable(loader):
            return {}
        try:
            policy = loader(message.connection_id, message.sender_id)
        except Exception:
            LOGGER.exception("User capability policy lookup failed")
            return {}
        return dict(policy) if isinstance(policy, dict) else {}


def _append_line(text: str, line: str) -> str:
    return f"{text.rstrip()}\n\n{line}" if text.strip() else line


def _message_payload(message: IncomingMessage) -> dict[str, object]:
    return {
        "connection_id": message.connection_id,
        "message_id": message.message_id,
        "sender_id": message.sender_id,
        "chat_id": message.chat_id,
        "chat_type": message.chat_type,
        "session_id": message.session_id,
        "task_id": message.task_id,
        "trace_id": message.trace_id,
        "content": message.content,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
