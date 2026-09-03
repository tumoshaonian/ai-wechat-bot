"""Bridge-safe facade and logging integration tests."""

import logging
import tempfile
import unittest
from pathlib import Path

from wechat_agent.admin.events import AdminEventRecorder
from wechat_agent.admin.logging_handler import AdminLogHandler
from wechat_agent.admin.security import SecretBox
from wechat_agent.admin.store import AdminStore


class AdminEventsTests(unittest.TestCase):
    def test_unavailable_recorder_fails_open(self) -> None:
        recorder = AdminEventRecorder(None, initialization_error=RuntimeError("offline"))
        self.assertTrue(recorder.claim_message("c", "m"))
        self.assertIsNone(recorder.record_event("task.started"))
        self.assertEqual([], recorder.claim_control_commands("worker"))

    def test_log_handler_redacts_and_does_not_recurse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AdminStore(root / "admin.db", SecretBox.load(root / "key"))
            recorder = AdminEventRecorder(store)
            logger = logging.getLogger(f"test.admin.{id(self)}")
            logger.handlers.clear()
            logger.propagate = False
            logger.setLevel(logging.INFO)
            logger.addHandler(AdminLogHandler(recorder, service="test"))
            logger.info("authorization=Bearer abcdefghijklmnop")
            logs = store.list_page("logs", page=1, page_size=20)
            self.assertEqual(1, logs["total"])
            self.assertNotIn("abcdefghijklmnop", logs["items"][0]["message"])


if __name__ == "__main__":
    unittest.main()
