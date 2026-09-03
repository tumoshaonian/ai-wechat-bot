"""Application-layer behavior tests without external SDK dependencies."""

import asyncio
import unittest
from pathlib import Path

from wechat_agent.application import MessageProcessor
from wechat_agent.domain import AgentReply, IncomingMessage, UserVisibleError


class FakeBackend:
    def __init__(
        self,
        reply: str | AgentReply = "answer",
        error: Exception | None = None,
    ) -> None:
        self.response = reply
        self.error = error
        self.messages: list[IncomingMessage] = []
        self.closed = False

    async def reply(self, message: IncomingMessage) -> str | AgentReply:
        self.messages.append(message)
        if self.error is not None:
            raise self.error
        return self.response

    async def close(self) -> None:
        self.closed = True


class FakeResponder:
    def __init__(self) -> None:
        self.updates: list[tuple[str, bool]] = []
        self.files: list[Path] = []

    async def send(self, text: str, *, finish: bool) -> None:
        self.updates.append((text, finish))

    async def send_file(self, path: Path) -> None:
        self.files.append(path)


def message(message_id: str = "m-1", sender_id: str = "owner") -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        sender_id=sender_id,
        chat_id=sender_id,
        chat_type="single",
        content="hello",
    )


class MessageProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def test_reports_progress_then_final_reply(self) -> None:
        backend = FakeBackend("hello back")
        responder = FakeResponder()
        processor = MessageProcessor(backend)

        await processor.handle(message(), responder)

        self.assertEqual(
            [("收到，正在处理…", False), ("hello back", True)],
            responder.updates,
        )
        self.assertEqual(1, len(backend.messages))

    async def test_refreshes_progress_for_slow_backend(self) -> None:
        class SlowBackend(FakeBackend):
            async def reply(self, incoming: IncomingMessage) -> str:
                await asyncio.sleep(0.04)
                return "done"

        responder = FakeResponder()
        processor = MessageProcessor(
            SlowBackend(),
            progress_interval_seconds=0.01,
        )

        await processor.handle(message(), responder)

        self.assertTrue(any("已用时" in text for text, _finish in responder.updates))
        self.assertEqual(("done", True), responder.updates[-1])

    async def test_rejects_sender_outside_allowlist(self) -> None:
        backend = FakeBackend()
        responder = FakeResponder()
        processor = MessageProcessor(backend, allowed_user_ids=frozenset({"owner"}))

        await processor.handle(message(sender_id="stranger"), responder)

        self.assertEqual([("当前账号没有使用此机器人的权限。", True)], responder.updates)
        self.assertEqual([], backend.messages)

    async def test_ignores_duplicate_message(self) -> None:
        backend = FakeBackend()
        first = FakeResponder()
        duplicate = FakeResponder()
        processor = MessageProcessor(backend)

        await processor.handle(message(), first)
        await processor.handle(message(), duplicate)

        self.assertEqual([], duplicate.updates)
        self.assertEqual(1, len(backend.messages))

    async def test_finishes_stream_when_backend_fails(self) -> None:
        backend = FakeBackend(error=RuntimeError("offline"))
        responder = FakeResponder()
        processor = MessageProcessor(backend)

        await processor.handle(message(), responder)

        self.assertEqual(False, responder.updates[0][1])
        self.assertEqual(("任务处理失败，请查看电脑端日志后重试。", True), responder.updates[-1])

    async def test_shows_safe_backend_error(self) -> None:
        backend = FakeBackend(error=UserVisibleError("余额不足", code="QUOTA"))
        responder = FakeResponder()
        processor = MessageProcessor(backend)

        await processor.handle(message(), responder)

        self.assertEqual(("余额不足", True), responder.updates[-1])

    async def test_delivers_agent_files_before_finishing_stream(self) -> None:
        attachment = Path("D:/report.docx")
        backend = FakeBackend(reply=AgentReply("文档已找到。", (attachment,)))
        responder = FakeResponder()
        processor = MessageProcessor(backend)

        await processor.handle(message(), responder)

        self.assertEqual([attachment], responder.files)
        self.assertEqual(False, responder.updates[-2][1])
        self.assertEqual(True, responder.updates[-1][1])
        self.assertIn("已发送文件：report.docx", responder.updates[-1][0])

    async def test_control_command_bypasses_busy_conversation_lock(self) -> None:
        class ControllableBackend(FakeBackend):
            def __init__(self) -> None:
                super().__init__()
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def reply(self, incoming: IncomingMessage) -> str:
                self.started.set()
                await self.release.wait()
                return "done"

            async def handle_control(self, incoming: IncomingMessage) -> str | None:
                return "ended" if incoming.content == "end" else None

        backend = ControllableBackend()
        processor = MessageProcessor(backend)
        running = asyncio.create_task(processor.handle(message("m-1"), FakeResponder()))
        await backend.started.wait()
        control_responder = FakeResponder()

        await processor.handle(
            IncomingMessage("m-2", "owner", "owner", "single", "end"),
            control_responder,
        )

        self.assertEqual([("ended", True)], control_responder.updates)
        backend.release.set()
        await running

    async def test_progress_stream_failure_aborts_backend_without_second_reply(self) -> None:
        class AbortableBackend(FakeBackend):
            def __init__(self) -> None:
                super().__init__()
                self.aborted = False

            async def reply(self, incoming: IncomingMessage) -> str:
                await asyncio.Event().wait()
                return "unreachable"

            async def abort_session(self, _chat_session_id: str) -> None:
                self.aborted = True

        class ExpiredResponder(FakeResponder):
            async def send(self, text: str, *, finish: bool) -> None:
                if self.updates:
                    raise RuntimeError("stream update expired")
                await super().send(text, finish=finish)

        backend = AbortableBackend()
        responder = ExpiredResponder()
        processor = MessageProcessor(
            backend,
            progress_interval_seconds=0.01,
            task_timeout_seconds=1,
        )

        await processor.handle(message(), responder)

        self.assertTrue(backend.aborted)
        self.assertEqual([("收到，正在处理…", False)], responder.updates)

    async def test_task_deadline_aborts_before_wecom_stream_expiry(self) -> None:
        class AbortableBackend(FakeBackend):
            def __init__(self) -> None:
                super().__init__()
                self.aborted = False

            async def reply(self, incoming: IncomingMessage) -> str:
                await asyncio.Event().wait()
                return "unreachable"

            async def abort_session(self, _chat_session_id: str) -> None:
                self.aborted = True

        backend = AbortableBackend()
        responder = FakeResponder()
        processor = MessageProcessor(
            backend,
            progress_interval_seconds=0.01,
            task_timeout_seconds=0.04,
        )

        await processor.handle(message(), responder)

        self.assertTrue(backend.aborted)
        self.assertTrue(responder.updates[-1][1])
        self.assertIn("为避免企业微信消息通道过期", responder.updates[-1][0])
