import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from wechat_agent.domain import IncomingMessage
from wechat_agent.file_delivery import FileDeliverySession
from wechat_agent.conversation_journal import ConversationJournal


class Responder:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def send_file(self, path):
        self.calls.append(path)
        if self.fail:
            raise TimeoutError("ack lost")


class FileDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_duplicate_is_sent_once(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "成果.txt"
            path.write_text("hello", encoding="utf-8")
            responder = Responder()
            session = FileDeliverySession(IncomingMessage("1", "u", "u", "single", "发给我"), responder)
            a, b = await asyncio.gather(session.send(path), session.send(path))
            self.assertTrue(a["ok"] and b["ok"])
            self.assertEqual(1, len(responder.calls))

    async def test_unknown_receipt_is_not_retried(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "file.txt"
            path.write_text("hello")
            responder = Responder(fail=True)
            session = FileDeliverySession(IncomingMessage("1", "u", "u", "single", "发给我"), responder)
            self.assertEqual("unknown", (await session.send(path))["status"])
            self.assertEqual("unknown", (await session.send(path))["status"])
            self.assertEqual(1, len(responder.calls))

    async def test_permission_is_enforced_before_read(self):
        responder = Responder()
        message = IncomingMessage("1", "u", "u", "single", "发给我", access_policy={"can_read_files": False})
        result = await FileDeliverySession(message, responder).send(Path("D:/never-read.txt"))
        self.assertEqual("rejected", result["status"])
        self.assertEqual([], responder.calls)

    async def test_receipt_survives_session_reconstruction(self):
        with TemporaryDirectory() as root:
            path = Path(root) / "test.txt"
            path.write_text("hello")
            store = ConversationJournal(Path(root) / "history")
            msg = IncomingMessage("1", "u", "u", "single", "发给我", task_id="task")
            first = Responder()
            await FileDeliverySession(msg, first, store).send(path)
            second = Responder()
            receipt = await FileDeliverySession(msg, second, store).send(path)
            self.assertTrue(receipt["duplicate"])
            self.assertEqual([], second.calls)
