"""End-to-end runtime event tests without FastAPI or the WeCom SDK."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from wechat_agent.adapters.deepseek_harness import DeepSeekHarnessBackend
from wechat_agent.application import MessageProcessor
from wechat_agent.domain import AgentReply, IncomingMessage


class RecordingEvents:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []
        self.claims: set[tuple[str, str]] = set()

    def record_event(self, event_type: str, **kwargs):
        self.events.append((event_type, kwargs))
        return str(len(self.events))

    def claim_message(self, connection_id: str, message_id: str) -> bool:
        key = (connection_id, message_id)
        if key in self.claims:
            return False
        self.claims.add(key)
        return True


class EchoBackend:
    def __init__(self) -> None:
        self.calls: list[IncomingMessage] = []

    async def reply(self, message: IncomingMessage) -> str:
        self.calls.append(message)
        return "完成"

    async def close(self) -> None:
        return None


class Responder:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []

    async def send(self, text: str, *, finish: bool) -> None:
        self.messages.append((text, finish))

    async def send_file(self, path: Path) -> None:
        del path


class TelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_store_keeps_delivery_separate_from_lost_final_reply(self):
        from wechat_agent.admin.store import AdminStore
        from wechat_agent.admin.security import SecretBox
        for upload_fails in (False, True):
            with self.subTest(upload_fails=upload_fails), TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = AdminStore(root / "admin.db", SecretBox.load(root / "key"))
                class Recorder:
                    record_event = store.record_event
                    claim_message = store.claim_message
                path = root / "result.txt"
                path.write_text("test artifact", encoding="utf-8")
                class Backend(EchoBackend):
                    async def reply(self, message):
                        self.calls.append(message)
                        return AgentReply("文档已生成", files=(path,))
                class Channel(Responder):
                    async def send(self, text, *, finish):
                        if finish:
                            raise RuntimeError("final ack lost")
                    async def send_file(self, path):
                        if upload_fails:
                            raise RuntimeError("upload ack lost")
                backend = Backend()
                incoming = IncomingMessage("integration", "owner", "owner", "single", "create and send")
                await MessageProcessor(backend, event_recorder=Recorder()).handle(incoming, Channel())
                task = store.task_detail(backend.calls[0].task_id)
                self.assertEqual("PARTIAL_SUCCEEDED", task["status"])
                self.assertEqual("文档已生成", task["result_summary"])
                self.assertEqual("succeeded", task["outcome"]["execution_state"])
                self.assertEqual("unknown", task["outcome"]["response_status"])
                self.assertEqual("UNKNOWN" if upload_fails else "SENT", task["deliveries"][0]["status"])
                with store.database.connect() as db:
                    last = db.execute("SELECT status FROM messages WHERE direction='OUTBOUND' ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()[0]
                self.assertEqual("UNKNOWN", last)
                store.reconcile_interrupted_tasks()
                await MessageProcessor(backend, event_recorder=Recorder()).handle(incoming, Channel())
                self.assertEqual(1, len(backend.calls))

    async def test_final_response_failure_preserves_execution_and_prevents_replay(self):
        class FinalBrokenResponder(Responder):
            async def send(self, text, *, finish):
                if finish:
                    raise RuntimeError("ack lost")
                await super().send(text, finish=finish)
        recorder = RecordingEvents()
        backend = EchoBackend()
        message = IncomingMessage("final-lost", "owner", "owner", "single", "create document")
        await MessageProcessor(backend, event_recorder=recorder).handle(message, FinalBrokenResponder())
        events = [(name, data["payload"]) for name, data in recorder.events]
        execution = next(payload for name, payload in events if name == "agent.execution.completed")
        terminal = next(payload for name, payload in events if name == "task.completed")
        self.assertEqual("完成", execution["result"])
        self.assertEqual("partial_succeeded", terminal["state"])
        self.assertEqual("unknown", terminal["response_status"])
        self.assertEqual("succeeded", terminal["execution_state"])
        self.assertEqual("完成", terminal["result"])
        await MessageProcessor(backend, event_recorder=recorder).handle(message, Responder())
        self.assertEqual(1, len(backend.calls))

    async def test_message_task_and_outbound_events_share_trace(self) -> None:
        recorder = RecordingEvents()
        backend = EchoBackend()
        processor = MessageProcessor(backend, event_recorder=recorder)
        incoming = IncomingMessage(
            "msg-1",
            "owner",
            "owner",
            "single",
            "执行任务",
            connection_id="connection-1",
        )

        await processor.handle(incoming, Responder())

        names = [name for name, _ in recorder.events]
        self.assertIn("message.received", names)
        self.assertIn("task.started", names)
        self.assertEqual(2, names.count("message.outbound"))
        self.assertIn("task.completed", names)
        traces = {
            kwargs.get("trace_id")
            for _name, kwargs in recorder.events
            if kwargs.get("trace_id")
        }
        self.assertEqual(1, len(traces))
        self.assertIsNotNone(backend.calls[0].task_id)
        self.assertIsNotNone(backend.calls[0].trace_id)

    async def test_response_failure_always_moves_task_to_terminal_state(self) -> None:
        class BrokenResponder(Responder):
            async def send(self, text: str, *, finish: bool) -> None:
                del text, finish
                raise RuntimeError("reply stream expired")

        recorder = RecordingEvents()
        backend = EchoBackend()
        processor = MessageProcessor(backend, event_recorder=recorder)

        await processor.handle(
            IncomingMessage(
                "msg-response-failure",
                "owner",
                "owner",
                "single",
                "执行任务",
                connection_id="connection-1",
            ),
            BrokenResponder(),
        )

        names = [name for name, _ in recorder.events]
        self.assertIn("task.started", names)
        self.assertIn("message.outbound.failed", names)
        self.assertIn("task.failed", names)
        self.assertEqual(0, len(backend.calls))

    async def test_durable_claim_prevents_reexecution_after_processor_restart(self) -> None:
        recorder = RecordingEvents()
        incoming = IncomingMessage(
            "msg-1",
            "owner",
            "owner",
            "single",
            "执行任务",
            connection_id="connection-1",
        )
        first_backend = EchoBackend()
        second_backend = EchoBackend()

        await MessageProcessor(first_backend, event_recorder=recorder).handle(
            incoming,
            Responder(),
        )
        await MessageProcessor(second_backend, event_recorder=recorder).handle(
            incoming,
            Responder(),
        )

        self.assertEqual(1, len(first_backend.calls))
        self.assertEqual(0, len(second_backend.calls))
        self.assertIn("message.duplicate", [name for name, _ in recorder.events])

    async def test_admin_user_policy_overrides_static_allowlist(self) -> None:
        class DenyingEvents(RecordingEvents):
            def authorize_wecom_user(
                self,
                connection_id,
                external_user_id,
                *,
                bootstrap_allowed,
            ):
                del connection_id, external_user_id, bootstrap_allowed
                return False, "user_disabled"

        recorder = DenyingEvents()
        backend = EchoBackend()
        responder = Responder()
        processor = MessageProcessor(
            backend,
            allowed_user_ids=frozenset({"owner"}),
            event_recorder=recorder,
        )

        await processor.handle(
            IncomingMessage(
                "msg-disabled",
                "owner",
                "owner",
                "single",
                "执行任务",
                connection_id="connection-1",
            ),
            responder,
        )

        self.assertEqual([], backend.calls)
        self.assertIn("没有使用", responder.messages[-1][0])
        rejected = next(
            kwargs
            for name, kwargs in recorder.events
            if name == "message.rejected"
        )
        self.assertEqual("user_disabled", rejected["payload"]["reason"])

    async def test_harness_notifications_create_tool_timeline(self) -> None:
        class NotifyingHarness:
            def run(self, input: str, *, session_id: str, on_notification=None):
                del input
                on_notification(
                    SimpleNamespace(
                        method="session.event",
                        payload={
                            "sessionId": session_id,
                            "event": {
                                "type": "tool/call",
                                "data": {
                                    "callId": "call-1",
                                    "name": "mcp__desktop__capture",
                                },
                            },
                        },
                    )
                )
                on_notification(
                    SimpleNamespace(
                        method="session.event",
                        payload={
                            "sessionId": session_id,
                            "event": {
                                "type": "tool/result",
                                "data": {
                                    "message": {
                                        "source": {"callId": "call-1"},
                                        "content": [{"isError": False}],
                                    }
                                },
                            },
                        },
                    )
                )
                return SimpleNamespace(
                    final_response="工具执行完成",
                    finish_reason="completed",
                    events=[],
                )

            def close(self) -> None:
                return None

        with TemporaryDirectory() as temporary:
            recorder = RecordingEvents()
            backend = DeepSeekHarnessBackend(
                SimpleNamespace(harness_session_root=Path(temporary)),
                harness_factory=lambda _settings: NotifyingHarness(),
                event_recorder=recorder,
            )
            incoming = IncomingMessage(
                "msg-2",
                "owner",
                "owner",
                "single",
                "截图",
                connection_id="connection-1",
                task_id="task-1",
                trace_id="trace-1",
            )

            await backend.reply(incoming)
            await backend.close()

        names = [name for name, _ in recorder.events]
        self.assertIn("agent.session.started", names)
        self.assertIn("tool.started", names)
        self.assertIn("tool.completed", names)
        self.assertIn("agent.session.completed", names)


if __name__ == "__main__":
    unittest.main()
