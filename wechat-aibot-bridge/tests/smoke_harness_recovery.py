"""Explicit real-SDK smoke: isolated history, no WeCom sending or desktop actions."""
import asyncio
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from wechat_agent.config import Settings
from wechat_agent.adapters.deepseek_harness import DeepSeekHarnessBackend
from wechat_agent.domain import IncomingMessage
from wechat_agent.file_delivery import FileDeliverySession


async def main():
    settings = Settings.from_environment()
    with tempfile.TemporaryDirectory(prefix="wecom-harness-smoke-") as root:
        settings = replace(settings, harness_session_root=Path(root) / "registry", harness_dsh_home=Path(root) / "home", harness_workspace=Path(root))
        first = DeepSeekHarnessBackend(settings)
        try:
            await first.start()
            print("SDK initialized with current profile + desktop MCP", flush=True)
            message = IncomingMessage("smoke-1", "test", "test", "single", "这是只读记忆测试。不要调用任何工具。请记住测试代号为蓝鲸731，只回复已记住。", task_id="smoke-1")
            result = await first.reply(message)
            print(json.dumps({"first": result.text}, ensure_ascii=False), flush=True)
            first.record_delivery(message, {"sent_files": ["蓝鲸731-回归测试.txt"], "test_receipt": True})
            artifact = Path(root) / "蓝鲸731-回归测试.txt"
            artifact.write_text("isolated delivery regression", encoding="utf-8")
            delivered = []
            class TestChannel:
                async def send_file(self, path):
                    assert path.read_text(encoding="utf-8") == "isolated delivery regression"
                    delivered.append(path)
            delivery_message = IncomingMessage("smoke-deliver", "test", "test", "single", f"这是隔离交付测试。请只调用 mcp__desktop__deliver_file，把已有文件 {artifact} 发送给我。不要调用其他工具。", task_id="smoke-deliver")
            delivery = FileDeliverySession(delivery_message, TestChannel(), first.delivery_store)
            unbind = first.bind_delivery(delivery_message, delivery.send)
            try:
                reply = await first.reply(delivery_message)
            finally:
                unbind()
            assert delivered == [artifact], f"expected one mock-channel delivery, got {len(delivered)}"
            print(json.dumps({"delivery_tool": reply.text, "mock_channel_calls": len(delivered)}, ensure_ascii=False), flush=True)
        finally:
            await first.close()
        second = DeepSeekHarnessBackend(settings)
        try:
            reply = await second.reply(IncomingMessage("smoke-2", "test", "test", "single", "不要调用工具。测试代号是什么？渠道测试回执记录发送了哪个文件？", task_id="smoke-2"))
            print(json.dumps({"recovered": reply.text}, ensure_ascii=False), flush=True)
            assert "蓝鲸731" in reply.text and "回归测试.txt" in reply.text
            await second.end_session(message.session_id)
            reply = await second.reply(IncomingMessage("smoke-3", "test", "test", "single", "不要调用工具。当前对话里是否告诉过你测试代号？不知道就只回答不知道。", task_id="smoke-3"))
            print(json.dumps({"after_end": reply.text}, ensure_ascii=False), flush=True)
            assert "不知道" in reply.text and "蓝鲸731" not in reply.text
        finally:
            await second.close()


if __name__ == "__main__":
    asyncio.run(main())
