import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from wechat_agent.adapters.unified_agent import UnifiedAgentBackend
from wechat_agent.domain import AgentReply, IncomingMessage


class FakeHarnessBackend:
    def __init__(self) -> None:
        self.messages: list[IncomingMessage] = []
        self.ended: list[str] = []
        self.stopped: list[str] = []
        self.closed = False

    async def reply(self, message: IncomingMessage) -> str:
        self.messages.append(message)
        return f"agent:{message.content}"

    async def end_session(self, session_id: str):
        self.ended.append(session_id)
        return False, SimpleNamespace(generation=2)

    async def stop_session(self, session_id: str):
        self.stopped.append(session_id)
        return True, SimpleNamespace(generation=3)

    def session_status(self, _session_id: str):
        return SimpleNamespace(generation=1)

    def is_busy(self, _session_id: str) -> bool:
        return False

    async def close(self) -> None:
        self.closed = True


def message(content: str) -> IncomingMessage:
    return IncomingMessage("m-1", "owner", "owner", "single", content)


class UnifiedAgentBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.harness = FakeHarnessBackend()
        self.agent = UnifiedAgentBackend(self.harness)  # type: ignore[arg-type]

    async def test_plain_chat_and_computer_requests_use_same_agent(self) -> None:
        chat = await self.agent.reply(message("什么是计算机网络"))
        action = await self.agent.reply(message("打开记事本"))

        self.assertEqual(chat, "agent:什么是计算机网络")
        self.assertEqual(action, "agent:打开记事本")
        self.assertEqual(len(self.harness.messages), 2)

    async def test_legacy_computer_prefix_is_optional_and_stripped(self) -> None:
        reply = await self.agent.reply(message("/电脑：创建项目"))

        self.assertEqual(reply, "agent:创建项目")

    async def test_end_rotates_session_without_calling_agent(self) -> None:
        reply = await self.agent.handle_control(message("end"))

        self.assertIn("g0002", reply or "")
        self.assertEqual(len(self.harness.ended), 1)
        self.assertEqual(self.harness.messages, [])

    async def test_force_chat_adds_no_tool_constraint(self) -> None:
        await self.agent.reply(message("/聊天 怎么打开记事本"))

        self.assertIn("不得调用任何工具", self.harness.messages[0].content)

    async def test_direct_file_command_bypasses_harness(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.docx"
            path.write_bytes(b"document")

            reply = await self.agent.reply(message(f'/文件 "{path}"'))

            self.assertEqual(AgentReply("已找到文件，准备发送：report.docx", (path.resolve(),)), reply)
            self.assertEqual([], self.harness.messages)


if __name__ == "__main__":
    unittest.main()
