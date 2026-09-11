"""Real processor/adapter/HTTP broker, simulated model and channel; no GUI actions."""
import asyncio
import json
import re
import unittest
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from wechat_agent.adapters.deepseek_harness import DeepSeekHarnessBackend
from wechat_agent.adapters.unified_agent import UnifiedAgentBackend
from wechat_agent.application import MessageProcessor
from wechat_agent.delivery_broker import DeliveryBroker
from wechat_agent.domain import IncomingMessage
from test_telemetry import RecordingEvents, Responder


class ConfirmationFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_approve_reject_expire_and_end_through_control_lane(self):
        for decision in ("approve", "reject", "expire", "end", "stop"):
            with self.subTest(decision=decision), TemporaryDirectory() as root:
                record = RecordingEvents()
                presented = asyncio.Event()
                codes = []
                effects = []
                class Runtime:
                    closed = False
                    def run(self, content, *, session_id):
                        ticket = backend._task_tickets["original"]
                        request = urllib.request.Request(backend._broker.url.rsplit("/", 1)[0] + "/confirm",
                            data=json.dumps({"task_ticket": ticket, "operation": {"action": "测试动作", "target": "虚拟对象", "effect": "仅测试，不操作电脑"}}).encode(),
                            headers={"Authorization": "Bearer " + backend._broker.token})
                        with urllib.request.urlopen(request, timeout=3) as response:
                            result = json.load(response)
                        if result.get("approved") and not self.closed:
                            effects.append("simulated action")
                        return SimpleNamespace(final_response="done", finish_reason="completed")
                    def close(self):
                        self.closed = True
                runtime = Runtime()
                backend = DeepSeekHarnessBackend(SimpleNamespace(harness_session_root=Path(root)), harness_factory=lambda _: runtime, event_recorder=record)
                backend._broker = DeliveryBroker(backend._deliver_from_tool, backend._confirm_from_tool)
                backend._confirmations.timeout = 0.05 if decision == "expire" else 2
                processor = MessageProcessor(UnifiedAgentBackend(backend), event_recorder=record, progress_interval_seconds=0.01)
                class Channel(Responder):
                    async def send(self, text, *, finish):
                        await super().send(text, finish=finish)
                        match = re.search(r"/确认 ([0-9a-f]{16})", text)
                        if match:
                            codes.append(match[1])
                            presented.set()
                def message(task_id, text):
                    return IncomingMessage(task_id, "owner", "owner", "single", text, task_id=task_id)
                task = asyncio.create_task(processor.handle(message("original", "ask"), Channel()))
                try:
                    await asyncio.wait_for(presented.wait(), 2)
                    self.assertFalse(task.done())
                    # A bare confirmation never releases the waiter.
                    await processor.handle(message("bare", "确认"), Responder())
                    self.assertFalse(task.done())
                    if decision != "expire":
                        command = {"approve": "/确认 " + codes[0], "reject": "/拒绝 " + codes[0], "end": "end", "stop": "stop"}[decision]
                        await asyncio.wait_for(processor.handle(message("decision", command), Responder()), 2)
                    await asyncio.wait_for(task, 3)
                    self.assertEqual(["simulated action"] if decision == "approve" else [], effects)
                    if decision != "approve":
                        self.assertTrue(runtime.closed)
                    self.assertFalse(backend.waiting_confirmation(message("original", "").session_id))
                    terminal = [kind for kind, data in record.events if data.get("payload", {}).get("task_id") == "original" and kind in {"task.completed", "task.cancelled"}]
                    self.assertEqual(["task.completed"] if decision == "approve" else ["task.cancelled"], terminal)
                finally:
                    await processor.close()
                    await asyncio.gather(task, return_exceptions=True)
