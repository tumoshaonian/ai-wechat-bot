import asyncio
import sqlite3
from contextlib import closing
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from wechat_agent.conversation_journal import ConversationJournal
from wechat_agent.domain import AgentTaskInterrupted, UserVisibleError
from wechat_agent.history_recovery import recover_context


class HistoryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = ConversationJournal(Path(self.temp.name))
        for i in range(10):
            self.journal.append("chat", 1, "user", f"轮次{i}: " + "旧记录" * 90)
            self.journal.append("chat", 1, "assistant", {"path": f"D:/项目/{i}", "text": "已准备，但交付尚未确认"})

    async def recover(self, summarize):
        return await recover_context(self.journal, "chat", 1, 0, 4096, summarize, lambda _: None)

    async def test_summary_and_recent_turns_survive_restart_without_deleting_originals(self):
        prompts = []
        async def summarize(prompt):
            prompts.append(prompt)
            self.assertLessEqual(len(prompt.encode("utf-8")), 4096)
            return "早期项目保存在 D:/项目；模型报告已准备，渠道未确认交付，不得重复发送。"
        text, last = await self.recover(summarize)
        self.assertLessEqual(len(text.encode("utf-8")), 4096)
        self.assertIn("history_summary", text)
        self.assertIn("轮次9", text)
        self.assertIn("D:/项目/9", text)
        self.assertGreater(len(prompts), 0)
        reopened = ConversationJournal(Path(self.temp.name))
        self.assertEqual((text, last), reopened.context("chat", 1, 0, 4096))
        with closing(sqlite3.connect(reopened.path)) as db:
            self.assertEqual(20, db.execute("SELECT count(*) FROM facts").fetchone()[0])

    async def test_summary_failure_does_not_publish_checkpoint(self):
        async def fail(_):
            raise RuntimeError("provider unavailable")
        with self.assertRaises(UserVisibleError) as caught:
            await self.recover(fail)
        self.assertEqual("HISTORY_SUMMARY_FAILED", caught.exception.code)
        self.assertNoCheckpoint()

    def assertNoCheckpoint(self):
        with closing(sqlite3.connect(self.journal.path)) as db:
            self.assertEqual(0, db.execute("SELECT count(*) FROM checkpoints").fetchone()[0])

    async def test_invalid_summary_is_not_saved(self):
        async def oversized(_):
            return "x" * 2000
        with self.assertRaises(UserVisibleError) as caught:
            await self.recover(oversized)
        self.assertEqual("HISTORY_SUMMARY_INVALID", caught.exception.code)
        self.assertNoCheckpoint()

    async def test_end_during_summary_rejects_old_epoch_commit(self):
        async def ending(_):
            self.journal.end("chat")
            return "旧会话摘要"
        with self.assertRaises(AgentTaskInterrupted):
            await self.recover(ending)
        self.assertNoCheckpoint()
        self.assertEqual(("", 0), self.journal.context("chat", 2, 0, 4096))

    async def test_late_delivery_receipt_is_not_swallowed_by_checkpoint(self):
        async def summarize(_):
            self.journal.append("chat", 1, "delivery", {"status": "unknown", "name": "晚到回执.zip"})
            return "历史摘要，交付未知，不能重复执行。"
        text, _ = await self.recover(summarize)
        self.assertIn("晚到回执.zip", text)
        self.assertIn('"status": "unknown"', text)

    async def test_cancellation_leaves_history_intact(self):
        started = asyncio.Event()
        async def blocked(_):
            started.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(self.recover(blocked))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNoCheckpoint()

    async def test_one_oversized_turn_is_not_split(self):
        self.journal.append("other", 1, "user", "大" * 2000)
        async def forbidden(_):
            self.fail("must not split the newest indivisible turn")
        with self.assertRaises(UserVisibleError):
            await recover_context(self.journal, "other", 1, 0, 4096, forbidden, lambda _: None)

    async def test_checkpoint_cannot_cover_another_chat(self):
        with self.assertRaises(ValueError):
            self.journal.save_checkpoint("another", 1, 0, 2, "wrong")
