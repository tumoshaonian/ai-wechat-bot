import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wechat_agent.adapters.harness_summary import summarize_history


class SummaryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_cancellation_waits_for_owned_runtime_cleanup(self):
        started = threading.Event()
        closing = threading.Event()
        release = threading.Event()
        paths = []

        class BlockingHarness:
            def run(self, *_, **__):
                started.set()
                release.wait(3)
                return SimpleNamespace(finish_reason="completed", final_response="summary")

            def close(self):
                closing.set()
                release.wait(3)
                self.cleaned = True

        harness = BlockingHarness()
        def factory(settings):
            paths.append(settings.harness_workspace)
            self.assertFalse(settings.desktop_tools_enabled)
            self.assertEqual(1, len(settings.harness_patch_files))
            return harness

        with patch("wechat_agent.adapters.harness_summary.replace", lambda _, **kw: SimpleNamespace(**kw)), patch(
            "wechat_agent.adapters.deepseek_harness._create_harness", factory,
        ):
            task = asyncio.create_task(summarize_history(None, "history only"))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                task.cancel()
                self.assertTrue(await asyncio.to_thread(closing.wait, 1))
                task.cancel()
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                self.assertTrue(paths[0].exists())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
            self.assertTrue(harness.cleaned)
            self.assertFalse(paths[0].exists())
