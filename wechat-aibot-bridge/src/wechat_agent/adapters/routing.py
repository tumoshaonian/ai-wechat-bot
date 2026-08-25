"""Route explicit computer-control commands to DeepSeek Harness."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from ..domain import IncomingMessage
from ..ports import ChatBackend


class RoutingChatBackend(ChatBackend):
    """Keep ordinary chat separate from explicit computer operations."""

    def __init__(
        self,
        chat_backend: ChatBackend,
        harness_backend: ChatBackend,
        *,
        harness_command_prefix: str,
    ) -> None:
        self._chat_backend = chat_backend
        self._harness_backend = harness_backend
        self._prefix = harness_command_prefix

    async def reply(self, message: IncomingMessage) -> str:
        content = message.content.strip()
        has_command_boundary = (
            content == self._prefix
            or (
                content.startswith(self._prefix)
                and content[len(self._prefix) : len(self._prefix) + 1] in {" ", "：", ":"}
            )
        )
        if not has_command_boundary:
            return await self._chat_backend.reply(message)

        command = content[len(self._prefix) :].lstrip(" ：:")
        if not command:
            return f"请在 {self._prefix} 后写明需要在电脑上完成的任务。"
        return await self._harness_backend.reply(replace(message, content=command))

    async def close(self) -> None:
        await asyncio.gather(
            self._chat_backend.close(),
            self._harness_backend.close(),
        )
