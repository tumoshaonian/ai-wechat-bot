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


def message(content: str, **kwargs) -> IncomingMessage:
    return IncomingMessage("m-1", "owner", "owner", "single", content, **kwargs)


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

    async def test_legacy_file_command_also_uses_harness(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.docx"
            path.write_bytes(b"document")

            reply = await self.agent.reply(message(f'/文件 "{path}"'))

            self.assertIn(str(path), self.harness.messages[0].content)
            self.assertIsInstance(reply, str)

    async def test_compound_and_simple_file_requests_are_not_intercepted(self) -> None:
        for content in (
            "请你打开电脑豆包，然后让豆包帮我在桌面创建一个文档，并且在里面写一个200字的故事，然后你把这个豆包生成的文档发给我",
            "给我发送电脑桌面的LeapMind暑假开发计划文档",
            "在未预置的科研应用星图中导出实验报告，然后把报告发送给我",
        ):
            await self.agent.reply(message(content))
            self.assertEqual(content, self.harness.messages[-1].content)

    async def test_user_policy_blocks_direct_file_and_computer_operations(self) -> None:
        denied = {
            "can_use_computer": False,
            "can_read_files": False,
            "can_send_files": False,
            "can_execute_commands": False,
        }
        file_reply = await self.agent.reply(
            message('/文件 "D:/report.docx"', access_policy=denied)
        )
        computer_reply = await self.agent.reply(
            message("/电脑 打开记事本", access_policy=denied)
        )
        await self.agent.reply(
            message("怎么打开记事本", access_policy=denied)
        )

        self.assertIn("没有读取", file_reply)
        self.assertIn("没有操作电脑", computer_reply)
        self.assertIn("不得调用任何电脑", self.harness.messages[-1].content)


if __name__ == "__main__":
    unittest.main()
