import unittest

from wechat_agent.adapters.routing import RoutingChatBackend
from wechat_agent.domain import IncomingMessage


class FakeBackend:
    def __init__(self, label: str) -> None:
        self.label = label
        self.messages: list[IncomingMessage] = []
        self.closed = False

    async def reply(self, message: IncomingMessage) -> str:
        self.messages.append(message)
        return f"{self.label}:{message.content}"

    async def close(self) -> None:
        self.closed = True


def message(content: str) -> IncomingMessage:
    return IncomingMessage("m-1", "owner", "owner", "single", content)


class RoutingChatBackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.chat = FakeBackend("chat")
        self.harness = FakeBackend("harness")
        self.router = RoutingChatBackend(
            self.chat,
            self.harness,
            harness_command_prefix="/电脑",
        )

    async def test_routes_normal_chat_to_chat_backend(self) -> None:
        reply = await self.router.reply(message("你好"))

        self.assertEqual(reply, "chat:你好")
        self.assertEqual(len(self.chat.messages), 1)
        self.assertEqual(self.harness.messages, [])

    async def test_strips_prefix_before_harness(self) -> None:
        reply = await self.router.reply(message("/电脑：打开记事本"))

        self.assertEqual(reply, "harness:打开记事本")
        self.assertEqual(self.harness.messages[0].content, "打开记事本")

    async def test_similar_text_without_boundary_remains_normal_chat(self) -> None:
        reply = await self.router.reply(message("/电脑游戏推荐"))

        self.assertEqual(reply, "chat:/电脑游戏推荐")
        self.assertEqual(self.harness.messages, [])

    async def test_empty_command_returns_usage_without_running_harness(self) -> None:
        reply = await self.router.reply(message("/电脑"))

        self.assertIn("写明", reply)
        self.assertEqual(self.harness.messages, [])

    async def test_closes_both_backends(self) -> None:
        await self.router.close()

        self.assertTrue(self.chat.closed)
        self.assertTrue(self.harness.closed)
