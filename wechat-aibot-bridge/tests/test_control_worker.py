import asyncio
import unittest
from pathlib import Path

from wechat_agent.control_worker import AdminControlWorker


class Store:
    def __init__(self, commands):
        self.commands = list(commands)
        self.completed = []

    def claim_control_commands(self, worker_id, *, command_types=None, limit=10):
        del worker_id, limit
        claimed = [
            item for item in self.commands if item["command_type"] in command_types
        ]
        self.commands = []
        return claimed

    def complete_control_command(self, command_id, **kwargs):
        self.completed.append((command_id, kwargs))
        return True

    def record_event(self, event_type, **kwargs):
        return f"{event_type}:{kwargs.get('resource_id')}"

    def get_delivery_retry_context(self, delivery_id):
        return {
            "id": delivery_id,
            "artifact_id": "artifact-1",
            "task_id": "task-1",
            "trace_id": "trace-1",
            "retry_count": 1,
            "path": "D:/artifact.txt",
            "external_chat_id": "owner",
            "connection_id": "connection-1",
        }


class Backend:
    def __init__(self):
        self.cancelled = []
        self.ended = []

    async def cancel_task(self, task_id):
        self.cancelled.append(task_id)
        return {"interrupted": True, "generation": 2, "state": "idle"}

    async def end_chat_session(self, session_id):
        self.ended.append(session_id)
        return {"interrupted": False, "generation": 3, "state": "idle"}


class AdminControlWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_executes_cancel_and_end_commands(self):
        store = Store(
            [
                {
                    "id": "c1",
                    "command_type": "CANCEL_TASK",
                    "target_id": "task-1",
                    "payload": {},
                },
                {
                    "id": "c2",
                    "command_type": "END_SESSION",
                    "target_id": "conversation-1",
                    "payload": {"session_id": "wecom:single:owner"},
                },
            ]
        )
        backend = Backend()
        worker = AdminControlWorker(store, backend, poll_seconds=0.01)

        await worker.start()
        for _ in range(50):
            if len(store.completed) == 2:
                break
            await asyncio.sleep(0.01)
        await worker.close()

        self.assertEqual(["task-1"], backend.cancelled)
        self.assertEqual(["wecom:single:owner"], backend.ended)
        self.assertTrue(all(item[1]["success"] for item in store.completed))

    async def test_reports_stale_task_as_failed_command(self):
        class StaleBackend(Backend):
            async def cancel_task(self, task_id):
                del task_id
                return {"interrupted": False, "state": "not_running"}

        store = Store(
            [
                {
                    "id": "c1",
                    "command_type": "CANCEL_TASK",
                    "target_id": "old-task",
                    "payload": {},
                }
            ]
        )
        worker = AdminControlWorker(store, StaleBackend(), poll_seconds=0.01)

        await worker.start()
        for _ in range(50):
            if store.completed:
                break
            await asyncio.sleep(0.01)
        await worker.close()

        self.assertFalse(store.completed[0][1]["success"])
        self.assertIn("no longer running", store.completed[0][1]["error"])

    async def test_redelivers_file_through_active_wecom_channel(self):
        class Sender:
            def __init__(self):
                self.calls = []

            async def send_file_to_chat(self, path, chat_id, *, connection_id):
                self.calls.append((path, chat_id, connection_id))
                return {"sent": True, "size_bytes": 12}

        store = Store(
            [{
                "id": "c-file",
                "command_type": "RESEND_FILE",
                "target_id": "delivery-2",
                "payload": {},
            }]
        )
        sender = Sender()
        worker = AdminControlWorker(
            store,
            Backend(),
            file_sender=sender,
            poll_seconds=0.01,
        )

        await worker.start()
        for _ in range(50):
            if store.completed:
                break
            await asyncio.sleep(0.01)
        await worker.close()

        self.assertEqual(
            [(Path("D:/artifact.txt"), "owner", "connection-1")],
            sender.calls,
        )
        self.assertTrue(store.completed[0][1]["success"])


if __name__ == "__main__":
    unittest.main()
