"""Real Harness/model/guard/broker; simulated owner, isolated test-only effect."""
import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from wechat_agent.adapters.deepseek_harness import DeepSeekHarnessBackend, _create_harness
from wechat_agent.adapters.unified_agent import UnifiedAgentBackend
from wechat_agent.config import Settings
from wechat_agent.domain import IncomingMessage, AgentTaskInterrupted
from wechat_agent.execution_policy import ExecutionPolicy


async def check(approve, blocked=False):
    settings = Settings.from_environment()
    bridge = Path(__file__).resolve().parents[1]
    with TemporaryDirectory(prefix='execution-policy-smoke-') as temporary:
        root = Path(temporary)
        patch = root / 'probe.yml'
        patch.write_text('- insert:\n    - id: policy-probe\n      name: ' + json.dumps(
            (bridge / 'tests' / 'policy-effect-probe.mjs').as_uri()) + '\n', encoding='utf-8')
        settings = replace(settings, harness_session_root=root/'journal', harness_dsh_home=root/'home',
            harness_workspace=root, desktop_tools_enabled=False, harness_patch_files=(patch,),
            execution_policy=ExecutionPolicy.parse({'confirmation_timeout_seconds': 30,
                'rules': {'policy_test_effect': 'deny'} if blocked else {}}))
        backend = DeepSeekHarnessBackend(settings)
        from wechat_agent.delivery_broker import DeliveryBroker
        backend._broker = DeliveryBroker(backend._deliver_from_tool, backend._confirm_from_tool, backend._authorize_dispatch)
        proof = root / 'effect.json'
        backend._harness_factory = lambda config: _create_harness(config, delivery_env={
            'DSH_DELIVERY_URL': backend._broker.url, 'DSH_DELIVERY_TOKEN': backend._broker.token,
            'DSH_EXECUTION_POLICY_URL': backend._broker.url.rsplit('/', 1)[0] + '/authorize',
            'DSH_EXECUTION_POLICY_NONCE': backend._runtime_nonce, 'WECOM_POLICY_PROOF': str(proof),
        })
        agent = UnifiedAgentBackend(backend)
        message = IncomingMessage('smoke', 'test-owner', 'test-chat', 'single',
            '这是隔离集成测试。只调用一次 policy_test_effect，参数 label=验收。'
            '不要主动调用确认工具，不要调用其他工具或委派；执行前权限层会自动处理。成功后回复测试成功。', task_id='test-task')
        prompts = []
        async def present(text):
            assert not proof.exists(), 'effect occurred before user approval'
            assert 'policy_test_effect' in text and '验收' in text, text
            prompts.append(text)
            code = re.search(r'/确认 ([0-9a-f]{16})', text)[1]
            decision = replace(message, message_id='decision', task_id='decision',
                content=('/确认 ' if approve else '/拒绝 ') + code)
            await agent.handle_control(decision)
        async def no_delivery(_):
            raise AssertionError('no WeCom delivery allowed')
        unbind = backend.bind_delivery(message, no_delivery)
        unconfirm = backend.bind_confirmation(message, present)
        try:
            try:
                result = await backend.reply(message)
                assert approve or blocked, 'rejection must interrupt the task'
                assert result.text
            except AgentTaskInterrupted:
                assert not approve
            assert len(prompts) == (0 if blocked else 1), prompts
            assert proof.exists() == (approve and not blocked)
            if approve and not blocked:
                assert json.loads(proof.read_text()) == {'effects': 1}
            print(json.dumps({'approved': approve, 'policy_denied': blocked, 'confirmation_count': len(prompts),
                'effect_executed': proof.exists(), 'channel': 'simulated owner; no WeCom/desktop actions'}), flush=True)
        finally:
            unconfirm(); unbind()
            await backend.close()


async def main():
    await check(True)
    await check(False)
    await check(False, blocked=True)


if __name__ == '__main__':
    asyncio.run(main())
