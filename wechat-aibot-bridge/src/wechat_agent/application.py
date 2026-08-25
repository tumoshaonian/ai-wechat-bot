"""Transport-independent message processing use case."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from .domain import AgentReply, AgentTaskInterrupted, IncomingMessage, UserVisibleError
from .ports import ChatBackend, ConversationResponder


LOGGER = logging.getLogger(__name__)


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


class MessageProcessor:
    """Authorize, serialize, execute, and report one inbound message."""

    def __init__(
        self,
        backend: ChatBackend,
        *,
        allowed_user_ids: frozenset[str] = frozenset(),
        progress_interval_seconds: float = 30,
    ) -> None:
        self._backend = backend
        self._allowed_user_ids = allowed_user_ids
        self._recent_ids = RecentMessageIds()
        self._conversation_locks: dict[str, asyncio.Lock] = {}
        self._progress_interval_seconds = max(0.01, progress_interval_seconds)

    async def handle(
        self,
        message: IncomingMessage,
        responder: ConversationResponder,
    ) -> None:
        """Process one message and finish exactly one response stream."""

        if self._allowed_user_ids and message.sender_id not in self._allowed_user_ids:
            LOGGER.warning("Rejected message from unauthorized user_id=%s", message.sender_id)
            await responder.send("当前账号没有使用此机器人的权限。", finish=True)
            return
        if not self._recent_ids.mark_if_new(message.message_id):
            LOGGER.info("Ignored duplicate message_id=%s", message.message_id)
            return

        control_handler = getattr(self._backend, "handle_control", None)
        if callable(control_handler):
            try:
                control_reply = await control_handler(message)
            except AgentTaskInterrupted:
                LOGGER.info("Agent task stopped for message_id=%s", message.message_id)
                await responder.send("当前任务已停止。", finish=True)
                return
            except Exception:
                LOGGER.exception("Agent control command failed for message_id=%s", message.message_id)
                await responder.send("控制命令执行失败，请查看电脑端日志。", finish=True)
                return
            if control_reply is not None:
                await responder.send(control_reply, finish=True)
                return

        lock = self._conversation_locks.setdefault(message.session_id, asyncio.Lock())
        async with lock:
            await responder.send("收到，正在处理…", finish=False)
            try:
                raw_reply = await self._reply_with_progress(message, responder)
                reply = raw_reply if isinstance(raw_reply, AgentReply) else AgentReply(raw_reply)
                text = reply.text.strip()
                if not text and not reply.files:
                    raise RuntimeError("chat backend returned an empty reply")
            except AgentTaskInterrupted:
                LOGGER.info("Agent task stopped for message_id=%s", message.message_id)
                await responder.send("当前任务已停止。", finish=True)
                return
            except UserVisibleError as exc:
                LOGGER.warning(
                    "User-visible backend failure for message_id=%s code=%s: %s",
                    message.message_id,
                    exc.code,
                    exc,
                )
                await responder.send(exc.user_message, finish=True)
                return
            except Exception:
                LOGGER.exception("Message processing failed for message_id=%s", message.message_id)
                await responder.send("任务处理失败，请查看电脑端日志后重试。", finish=True)
                return
            delivered: list[str] = []
            failed: list[str] = []
            if reply.files:
                await responder.send(
                    f"任务已完成，正在发送 {len(reply.files)} 个文件…",
                    finish=False,
                )
                for path in reply.files:
                    try:
                        await responder.send_file(path)
                    except Exception:
                        failed.append(path.name)
                        LOGGER.exception(
                            "Failed to deliver file for message_id=%s path=%s",
                            message.message_id,
                            path,
                        )
                    else:
                        delivered.append(path.name)

            summary = text
            if delivered:
                summary = _append_line(summary, f"已发送文件：{', '.join(delivered)}")
            if failed:
                summary = _append_line(summary, f"文件发送失败：{', '.join(failed)}，请查看电脑端日志。")
            await responder.send(summary or "文件已发送。", finish=True)

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
                done, _ = await asyncio.wait(
                    (task,),
                    timeout=self._progress_interval_seconds,
                )
                if done:
                    break
                elapsed = max(1, round(time.monotonic() - started_at))
                await responder.send(
                    f"仍在处理，已用时约 {elapsed} 秒…",
                    finish=False,
                )
            return await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def close(self) -> None:
        """Release the configured backend."""

        await self._backend.close()


def _append_line(text: str, line: str) -> str:
    return f"{text.rstrip()}\n\n{line}" if text.strip() else line
