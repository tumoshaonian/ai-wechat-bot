import unittest
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from wechat_agent.conversation_journal import ConversationJournal
from wechat_agent.adapters.deepseek_harness import DeepSeekHarnessBackend
from wechat_agent.domain import IncomingMessage, UserVisibleError


class FakeRuntime:
    def __init__(self):
        self.calls = []

    def run(self, content, *, session_id):
        self.calls.append((content, session_id))
        return SimpleNamespace(final_response="已找到项目 D:/work/demo，准备交付", finish_reason="completed")

    def close(self):
        pass


class JournalTests(unittest.TestCase):
    def test_epochs_preserve_records_but_do_not_inherit_after_end(self):
        with TemporaryDirectory() as root:
            journal = ConversationJournal(Path(root))
            journal.append("chat", 1, "user", "项目路径 D:/work/项目")
            journal.end("chat")
            self.assertEqual(2, journal.epoch("chat"))
            self.assertEqual(("", 0), journal.context("chat", 2, 0, 1000))
            self.assertIn("项目", journal.context("chat", 1, 0, 1000)[0])

    def test_budget_does_not_silently_truncate(self):
        with TemporaryDirectory() as root:
            journal = ConversationJournal(Path(root))
            journal.append("chat", 1, "user", "重要路径" * 100)
            with self.assertRaises(UserVisibleError):
                journal.context("chat", 1, 0, 100)
            self.assertIn("重要路径", journal.context("chat", 1, 0, 5000)[0])

    def test_connection_namespace_is_unambiguous(self):
        a = IncomingMessage("1", "u", "u", "single", "hi", connection_id="a:b")
        b = IncomingMessage("1", "u", "b:single:u", "single", "hi", connection_id="a")
        self.assertNotEqual(a.session_id, b.session_id)


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_turn_does_not_close_next_chats_runtime(self):
        class FailedRuntime(FakeRuntime):
            def run(self, content, *, session_id):
                return SimpleNamespace(final_response="", finish_reason="error", events=[])
        class HealthyRuntime(FakeRuntime):
            closed = False
            def run(self, content, *, session_id):
                self.assert_open = not self.closed
                return super().run(content, session_id=session_id)
            def close(self):
                self.closed = True
        with TemporaryDirectory() as root:
            healthy = HealthyRuntime()
            factories = iter([FailedRuntime(), healthy])
            backend = DeepSeekHarnessBackend(SimpleNamespace(harness_session_root=Path(root)), harness_factory=lambda _: next(factories))
            results = await asyncio.gather(
                backend.reply(IncomingMessage("1", "a", "a", "single", "first")),
                backend.reply(IncomingMessage("2", "b", "b", "single", "second")),
                return_exceptions=True,
            )
            self.assertIsInstance(results[0], UserVisibleError)
            self.assertTrue(healthy.assert_open)
            self.assertFalse(healthy.closed)
            self.assertEqual("succeeded", results[1].status)
            await backend.close()

    async def test_delivery_receipt_and_restart_are_visible_but_end_is_empty(self):
        with TemporaryDirectory() as root:
            settings = SimpleNamespace(harness_session_root=Path(root))
            first = FakeRuntime()
            backend = DeepSeekHarnessBackend(settings, harness_factory=lambda _: first)
            msg = IncomingMessage("1", "u", "u", "single", "创建项目", task_id="t1")
            await backend.reply(msg)
            backend.record_delivery(msg, {"sent_files": ["项目.zip"]})
            await backend.close()
            second = FakeRuntime()
            restarted = DeepSeekHarnessBackend(settings, harness_factory=lambda _: second)
            followup = IncomingMessage("2", "u", "u", "single", "刚才发了什么", task_id="t2")
            await restarted.reply(followup)
            self.assertIn("项目.zip", second.calls[0][0])
            self.assertIn("D:/work/demo", second.calls[0][0])
            self.assertNotEqual(first.calls[0][1], second.calls[0][1])
            await restarted.end_session(msg.session_id)
            await restarted.reply(IncomingMessage("3", "u", "u", "single", "你好", task_id="t3"))
            self.assertEqual("你好", second.calls[-1][0])
            await restarted.close()

    async def test_same_runtime_receives_new_delivery_facts(self):
        with TemporaryDirectory() as root:
            runtime = FakeRuntime()
            backend = DeepSeekHarnessBackend(SimpleNamespace(harness_session_root=Path(root)), harness_factory=lambda _: runtime)
            msg = IncomingMessage("1", "u", "u", "single", "发送文件", task_id="t1")
            await backend.reply(msg)
            backend.record_delivery(msg, {"failed_or_unknown_files": ["结果.zip"]})
            await backend.reply(IncomingMessage("2", "u", "u", "single", "成功了吗", task_id="t2"))
            self.assertIn("failed_or_unknown_files", runtime.calls[-1][0])
            self.assertIn("结果.zip", runtime.calls[-1][0])
            await backend.close()
