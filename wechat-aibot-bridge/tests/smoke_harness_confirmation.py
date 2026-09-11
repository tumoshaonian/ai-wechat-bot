"""Opt-in real model/MCP confirmation handshake; no real user approval or GUI actions."""
import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from wechat_agent.adapters.deepseek_harness import DeepSeekHarnessBackend
from wechat_agent.adapters.unified_agent import UnifiedAgentBackend
from wechat_agent.config import Settings
from wechat_agent.domain import IncomingMessage


async def main():
    settings = Settings.from_environment()
    with TemporaryDirectory(prefix="confirmation-smoke-") as temporary:
        root = Path(temporary)
        settings = replace(settings, harness_session_root=root / "registry", harness_dsh_home=root / "home", harness_workspace=root)
        backend = DeepSeekHarnessBackend(settings)
        agent = UnifiedAgentBackend(backend)
        message = IncomingMessage("test", "smoke-owner", "smoke-chat", "single",
            "这是无副作用的确认工具集成测试。请只调用 request_confirmation，action=测试确认握手，target=虚拟测试对象，"
            "effect=不操作电脑不发送文件。拿到 approved=true 后只回复‘确认握手通过’。不要调用任何其他工具或实际执行操作。", task_id="smoke-task")
        decisions = []
        async def present(text):
            match = re.search(r"/确认 ([0-9a-f]{16})", text)
            assert match, "missing one-shot code"
            control = replace(message, message_id="test-decision", task_id="decision", content="/确认 " + match[1])
            decisions.append(await agent.handle_control(control))
        async def no_delivery(_):
            raise AssertionError("smoke must not deliver files")
        unbind = backend.bind_delivery(message, no_delivery)
        unbind_confirmation = backend.bind_confirmation(message, present)
        try:
            result = await backend.reply(message)
            assert len(decisions) == 1, decisions
            with backend._confirmations.db() as db:
                rows = db.execute("SELECT status,operation FROM confirmations").fetchall()
            assert len(rows) == 1 and rows[0]["status"] == "approved"
            assert "确认握手通过" in result.text, result.text
            print(json.dumps({"reply": result.text, "confirmation_count": len(rows), "status": rows[0]["status"], "channel": "simulated owner, not WeCom"}, ensure_ascii=False), flush=True)
        finally:
            unbind_confirmation()
            unbind()
            await backend.close()


if __name__ == "__main__":
    asyncio.run(main())
