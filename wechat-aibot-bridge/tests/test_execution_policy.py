import asyncio
import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from wechat_agent.adapters.deepseek_harness import DeepSeekHarnessBackend
from wechat_agent.domain import IncomingMessage
from wechat_agent.execution_policy import confirmation_operation, validate_dispatch
from wechat_agent.execution_policy import ExecutionPolicy


class ExecutionPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = TemporaryDirectory()
        self.backend = DeepSeekHarnessBackend(SimpleNamespace(harness_session_root=Path(self.tmp.name)))
        self.message = IncomingMessage('m', 'owner', 'chat', 'single', 'test', task_id='task')
        self.backend._active_task_id = 'task'
        self.backend._active_policy_session = 'runtime-session'
        self.unbind = self.backend.bind_delivery(self.message, lambda _: None)
        self.body = dict(runtime_nonce=self.backend._runtime_nonce, session_id='runtime-session',
            caller_session_id='runtime-session', call_id='call1', tool='bash', arguments={'command': 'echo test'})
        self.asked = []
        def confirm(ticket, operation):
            self.asked.append(operation)
            return {'approved': True, 'status': 'approved'}
        self.backend._confirm_from_tool = confirm

    async def asyncTearDown(self):
        self.unbind()
        await self.backend.close()
        self.tmp.cleanup()

    async def test_unknown_and_shell_require_exact_parameter_confirmation(self):
        for name in ('bash', 'write_file', 'mcp__desktop__invoke', 'future_unknown_tool', 'subagent'):
            body = {**self.body, 'tool': name, 'call_id': name}
            result = self.backend._authorize_dispatch(body)
            self.assertTrue(result['approved'])
            self.assertIn(name, self.asked[-1]['action'])
            self.assertIn('echo test', self.asked[-1]['effect'])

    async def test_observation_does_not_require_confirmation(self):
        for name in ('mcp__desktop__list_windows', 'mcp__desktop__inspect_window'):
            self.assertTrue(self.backend._authorize_dispatch({**self.body, 'tool': name, 'call_id': name})['approved'])
        self.assertEqual([], self.asked)

    async def test_configured_deny_never_asks_or_approves(self):
        self.backend._execution_policy = ExecutionPolicy.parse({'rules': {'bash': 'deny'}})
        self.assertFalse(self.backend._authorize_dispatch(self.body)['approved'])
        self.assertEqual([], self.asked)

    async def test_observation_can_be_changed_to_ask(self):
        self.backend._execution_policy = ExecutionPolicy.parse({'rules': {'mcp__desktop__inspect_window': 'ask'}})
        self.assertTrue(self.backend._authorize_dispatch({**self.body, 'tool': 'mcp__desktop__inspect_window'})['approved'])
        self.assertEqual(1, len(self.asked))

    async def test_replay_rejected_even_with_changed_arguments(self):
        self.backend._authorize_dispatch(self.body)
        with self.assertRaises(ValueError):
            self.backend._authorize_dispatch({**self.body, 'arguments': {'command': 'other'}})
        self.assertEqual(1, len(self.asked))

    async def test_stale_runtime_or_session_and_missing_channel_rejected(self):
        for key in ('runtime_nonce', 'session_id'):
            with self.assertRaises(ValueError):
                self.backend._authorize_dispatch({**self.body, key: 'old'})
        self.unbind()
        with self.assertRaises(ValueError):
            self.backend._authorize_dispatch(self.body)
        self.assertEqual([], self.asked)

    async def test_cancellation_during_approval_invalidates_result(self):
        def confirm(*_):
            self.backend._active_task_id = None
            return {'approved': True}
        self.backend._confirm_from_tool = confirm
        self.assertFalse(self.backend._authorize_dispatch(self.body)['approved'])

    async def test_concurrent_authorization_fails_closed(self):
        self.backend._policy_lock.acquire()
        try:
            self.assertFalse(self.backend._authorize_dispatch(self.body)['approved'])
            self.assertEqual([], self.asked)
        finally:
            self.backend._policy_lock.release()

    async def test_oversize_parameters_not_truncated_into_approval(self):
        with self.assertRaises(ValueError):
            self.backend._authorize_dispatch({**self.body, 'arguments': {'command': 'x' * 2000}})
        self.assertEqual([], self.asked)

    async def test_digest_covers_all_arguments_and_transport_ticket_hidden_from_prompt(self):
        changed = copy.deepcopy(self.body)
        changed['arguments']['command'] = 'other'
        self.assertNotEqual(validate_dispatch(self.body), validate_dispatch(changed))
        changed['arguments']['task_ticket'] = 'sensitive-ticket'
        self.assertNotIn('sensitive-ticket', str(confirmation_operation(changed)))
        with self.assertRaises(ValueError):
            validate_dispatch({**self.body, 'extra': True})
