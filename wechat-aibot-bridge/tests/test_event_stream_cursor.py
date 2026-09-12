"""The first live subscription starts at a snapshot; reconnects can replay gaps."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from wechat_agent.admin.api import API_PREFIX, create_app
from wechat_agent.admin.config import AdminSettings
from wechat_agent.admin.security import SecretBox
from wechat_agent.admin.store import AdminStore


class EventCursorTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_skips_history_but_preserves_later_events_and_reconnects(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = AdminStore(root/'admin.db', SecretBox.load(root/'key'))
            settings = AdminSettings(database_path=root/'admin.db', master_key_path=root/'key',
                                     runtime_control_enabled=False, sse_poll_seconds=0.001)
            app = create_app(settings, store)
            endpoint = next(r.endpoint for r in app.routes if getattr(r, 'path', '') == API_PREFIX + '/events/stream')
            class Request:
                async def is_disconnected(self):
                    return False
            store.record_event('task.started', payload={'task_id': 'old'})
            head = store.latest_event_sequence()
            response = await endpoint(Request(), {}, after=0, tail=True)
            iterator = response.body_iterator
            try:
                first = await anext(iterator)
                self.assertIn('event: cursor', first)
                self.assertEqual(head, json.loads(first.split('data: ')[1])['seq'])
                store.record_event('task.completed', payload={'task_id': 'new'})
                new = await anext(iterator)
                self.assertIn('task.completed', new)
                self.assertNotIn('"old"', new)
            finally:
                await iterator.aclose()
            response = await endpoint(Request(), {}, after=head, tail=False)
            try:
                self.assertIn('task.completed', await anext(response.body_iterator))
            finally:
                await response.body_iterator.aclose()
            response = await endpoint(Request(), {}, after=999999, tail=False)
            try:
                self.assertIn('event: cursor', await anext(response.body_iterator))
            finally:
                await response.body_iterator.aclose()
            response = await endpoint(Request(), {}, after=0, tail=False)
            try:
                self.assertIn('task.started', await anext(response.body_iterator))
            finally:
                await response.body_iterator.aclose()
