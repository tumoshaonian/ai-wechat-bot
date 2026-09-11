import asyncio
import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from wechat_agent.confirmations import ConfirmationCoordinator
from wechat_agent.conversation_journal import ConversationJournal
from wechat_agent.domain import IncomingMessage


class ConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.journal = ConversationJournal(Path(self.temp.name))
        self.service = ConfirmationCoordinator(self.journal, timeout_seconds=0.5)
        self.message = IncomingMessage("request", "owner", "chat", "single", "operation", task_id="task", connection_id="one")
        self.operation = {"action": "覆盖测试文档", "target": "D:/test-only/doc.txt", "effect": "旧内容将被替换"}
        self.events = []
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.temp.cleanup()

    async def start_request(self, notify=None):
        sent = asyncio.Event()
        async def present(text):
            self.assertIn("/确认", text)
            self.assertIn(self.operation["target"], text)
            sent.set()
        task = asyncio.create_task(self.service.request(self.message, 1, self.operation, notify or present,
            lambda kind, data: self.events.append((kind, data))))
        self.tasks.append(task)
        if notify is None:
            await sent.wait()
        code = next(iter(self.service.waiters)) if self.service.waiters else None
        return code, task

    async def test_approval_is_one_shot_and_bound_to_immutable_description(self):
        code, task = await self.start_request()
        self.assertTrue(self.service.decide(self.message, code, True)[0])
        self.assertFalse(self.service.decide(self.message, code, True)[0])
        result = await task
        self.assertTrue(result["approved"])
        self.assertEqual(self.operation, result["operation"])
        with self.service.db() as db:
            row = db.execute("SELECT * FROM confirmations WHERE id=?", (code,)).fetchone()
            self.assertEqual("approved", row["status"])
            self.assertEqual(self.operation, json.loads(row["operation"]))
            self.assertEqual(result["operation_digest"], row["digest"])

    async def test_other_sender_connection_or_chat_cannot_approve(self):
        code, task = await self.start_request()
        for other in (replace(self.message, sender_id="other"), replace(self.message, connection_id="two"), replace(self.message, chat_id="another")):
            self.assertFalse(self.service.decide(other, code, True)[0])
        self.assertFalse(task.done())
        self.service.decide(self.message, code, False)
        self.assertEqual("rejected", (await task)["status"])

    async def test_timeout_is_not_approval(self):
        self.service.timeout = 0.01
        code, task = await self.start_request()
        self.assertEqual("expired", (await task)["status"])
        self.assertFalse(self.service.decide(self.message, code, True)[0])
        self.assertNotIn("task.resumed", [kind for kind, _ in self.events])

    async def test_end_invalidates_old_epoch(self):
        code, task = await self.start_request()
        self.journal.end(self.message.session_id)
        self.assertFalse(self.service.decide(self.message, code, True)[0])
        self.service.cancel_task(self.message.task_id)
        self.assertEqual("cancelled", (await task)["status"])

    async def test_cancelled_wait_is_persisted_and_cannot_be_confirmed(self):
        code, task = await self.start_request()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        with self.service.db() as db:
            self.assertEqual("cancelled", db.execute("SELECT status FROM confirmations WHERE id=?", (code,)).fetchone()[0])
        self.assertFalse(self.service.decide(self.message, code, True)[0])

    async def test_restart_does_not_resume_or_approve_persisted_requests(self):
        code, task = await self.start_request()
        restarted = ConfirmationCoordinator(self.journal)
        self.assertFalse(restarted.decide(self.message, code, True)[0])
        self.assertFalse(task.done())

    async def test_notification_failure_never_approves(self):
        async def failed(_):
            raise RuntimeError("channel unavailable")
        _, task = await self.start_request(notify=failed)
        with self.assertRaises(RuntimeError):
            await task
        self.assertFalse(self.service.waiting(task_id="task"))
        with self.service.db() as db:
            self.assertEqual("cancelled", db.execute("SELECT status FROM confirmations").fetchone()[0])

    async def test_expired_decision_is_rejected_even_before_wait_timer_fires(self):
        code, task = await self.start_request()
        self.service.clock = lambda: float("inf")
        self.assertFalse(self.service.decide(self.message, code, True)[0])
        self.assertEqual("expired", (await task)["status"])
