"""Task ownership, queue cancellation and end boundaries without desktop effects."""
import asyncio
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from wechat_agent.application import MessageProcessor
from wechat_agent.control_worker import AdminControlWorker
from wechat_agent.domain import IncomingMessage
from test_telemetry import RecordingEvents, Responder


class Backend:
    def __init__(self):
        self.calls = []
        self.aborts = []
        self.ended = []
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def reply(self, message):
        self.calls.append(message.task_id)
        if message.content == "block":
            self.started.set()
            await self.release.wait()
        return "done"

    async def abort_session(self, session):
        self.aborts.append(session)

    async def end_chat_session(self, session):
        self.ended.append(session)
        return {"generation": 2, "state": "idle"}

    async def close(self):
        self.release.set()


class TaskControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = Backend()
        self.events = RecordingEvents()
        self.processor = MessageProcessor(self.backend, event_recorder=self.events)
        self.handles = []

    async def asyncTearDown(self):
        await self.processor.close()
        await asyncio.gather(*self.handles, return_exceptions=True)

    async def submit(self, task_id, *, content="run", chat="owner", connection="one"):
        message = IncomingMessage(task_id, "owner", chat, "single", content, connection_id=connection, task_id=task_id)
        handle = asyncio.create_task(self.processor.handle(message, Responder()))
        self.handles.append(handle)
        for _ in range(100):
            if task_id in self.processor._tasks or handle.done():
                break
            await asyncio.sleep(0)
        return message, handle

    def terminal(self, task_id):
        return [kind for kind, event in self.events.events if event.get("payload", {}).get("task_id") == task_id and kind in {"task.cancelled", "task.completed", "task.failed"}]

    async def test_admin_cancels_queued_identity_without_stopping_active_task(self):
        first, active = await self.submit("first", content="block")
        await self.backend.started.wait()
        queued, waiting = await self.submit("queued")
        worker = AdminControlWorker(self.events, self.processor)
        result = await asyncio.wait_for(worker._cancel_task("queued"), 2)
        self.assertTrue(result["interrupted"])
        await waiting
        self.assertEqual(["first"], self.backend.calls)
        self.assertEqual([], self.backend.aborts)
        self.assertEqual(["task.cancelled"], self.terminal("queued"))
        self.assertFalse(active.done())
        self.backend.release.set()
        await active
        self.assertEqual(["first"], self.backend.calls)

    async def test_end_drains_old_queue_and_next_request_uses_new_boundary(self):
        message, first = await self.submit("first", content="block")
        await self.backend.started.wait()
        _, second = await self.submit("old-queued")
        result = await asyncio.wait_for(self.processor.end_chat_session(message.session_id), 2)
        await asyncio.gather(first, second)
        self.assertEqual(2, result["cancelled_tasks"])
        self.assertEqual([message.session_id], self.backend.ended)
        self.assertNotIn("old-queued", self.backend.calls)
        _, fresh = await self.submit("fresh")
        await fresh
        self.assertEqual(["first", "fresh"], self.backend.calls)

    async def test_cancel_before_work_coroutine_starts_is_recorded(self):
        _, pending = await self.submit("early")
        result = await self.processor.cancel_task("early")
        self.assertTrue(result["interrupted"])
        await pending
        self.assertEqual([], self.backend.calls)
        self.assertEqual(["task.cancelled"], self.terminal("early"))

    async def test_different_connection_is_not_stopped_by_session_end(self):
        message, first = await self.submit("first", content="block", connection="one")
        await self.backend.started.wait()
        _, other = await self.submit("other", content="block", connection="two")
        await asyncio.sleep(0.01)
        await self.processor.end_chat_session(message.session_id)
        self.assertFalse(other.done())
        self.backend.release.set()
        await other
        self.assertEqual(["task.completed"], self.terminal("other"))

    async def test_stale_task_id_does_not_cancel_new_work(self):
        self.assertFalse((await self.processor.cancel_task("missing"))["interrupted"])
        _, first = await self.submit("first")
        await first
        _, second = await self.submit("second", content="block")
        await self.backend.started.wait()
        self.assertFalse((await self.processor.cancel_task("first"))["interrupted"])
        self.assertFalse(second.done())

    async def test_shutdown_drains_queue_without_execution(self):
        _, first = await self.submit("first", content="block")
        await self.backend.started.wait()
        _, queued = await self.submit("queued")
        await asyncio.wait_for(self.processor.close(), 2)
        await asyncio.gather(first, queued)
        self.assertEqual(["first"], self.backend.calls)
        self.assertEqual(["task.cancelled"], self.terminal("queued"))

    async def test_request_arriving_during_end_is_rejected_before_execution(self):
        ending = asyncio.Event()
        release = asyncio.Event()
        async def slow_end(session):
            ending.set()
            await release.wait()
            return {"generation": 2}
        self.backend.end_chat_session = slow_end
        message, completed = await self.submit("first")
        await completed
        end = asyncio.create_task(self.processor.end_chat_session(message.session_id))
        try:
            await ending.wait()
            _, racing = await self.submit("racing")
            await racing
            self.assertNotIn("racing", self.backend.calls)
            self.assertEqual(["task.cancelled"], self.terminal("racing"))
        finally:
            release.set()
            await end

    async def test_repeated_cancel_waits_for_executor_cleanup(self):
        draining = asyncio.Event()
        release = asyncio.Event()
        async def slow_abort(session):
            draining.set()
            await release.wait()
        self.backend.abort_session = slow_abort
        _, handle = await self.submit("first", content="block")
        await self.backend.started.wait()
        first_cancel = asyncio.create_task(self.processor.cancel_task("first"))
        await draining.wait()
        second_cancel = asyncio.create_task(self.processor.cancel_task("first"))
        try:
            await asyncio.sleep(0.01)
            self.assertFalse(first_cancel.done())
            self.assertFalse(second_cancel.done())
            self.assertEqual([], self.terminal("first"))
        finally:
            release.set()
            await asyncio.gather(first_cancel, second_cancel, handle)
        self.assertEqual(["task.cancelled"], self.terminal("first"))

    async def test_unified_end_with_real_backend_and_journal_discards_old_queue(self):
        from wechat_agent.adapters.deepseek_harness import DeepSeekHarnessBackend
        from wechat_agent.adapters.unified_agent import UnifiedAgentBackend
        started = threading.Event()
        release = threading.Event()
        calls = []
        class Runtime:
            def run(self, content, *, session_id):
                calls.append((content, session_id))
                if "block" in content:
                    started.set()
                    release.wait(3)
                return SimpleNamespace(final_response="done", finish_reason="completed")
            def close(self):
                release.set()
        with TemporaryDirectory() as root:
            backend = DeepSeekHarnessBackend(SimpleNamespace(harness_session_root=Path(root)), harness_factory=lambda _: Runtime())
            processor = MessageProcessor(UnifiedAgentBackend(backend), event_recorder=self.events)
            def incoming(task_id, content):
                return IncomingMessage(task_id, "owner", "owner", "single", content, task_id=task_id)
            first = asyncio.create_task(processor.handle(incoming("h-first", "block"), Responder()))
            second = None
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                second = asyncio.create_task(processor.handle(incoming("h-queued", "old queued work"), Responder()))
                while "h-queued" not in processor._tasks:
                    await asyncio.sleep(0)
                response = Responder()
                await asyncio.wait_for(processor.handle(incoming("h-end", "end"), response), 2)
                await asyncio.gather(first, second)
                self.assertEqual(1, len(calls))
                self.assertEqual(2, backend.delivery_store.epoch(incoming("h-first", "").session_id))
                self.assertIn("2 个", response.messages[-1][0])
                await processor.handle(incoming("h-new", "fresh"), Responder())
                self.assertEqual(2, len(calls))
                self.assertNotIn("old queued work", calls[-1][0])
                self.assertNotIn("block", calls[-1][0])
            finally:
                release.set()
                await processor.close()
                await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
