"""One user-facing agent that decides whether a message needs tools."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

from ..domain import AgentReply, IncomingMessage
from ..file_requests import DesktopFileRequestResolver
from .deepseek_harness import DeepSeekHarnessBackend


class UnifiedAgentBackend:
    """Expose one natural-language agent while retaining explicit control commands."""

    _END_COMMANDS = {
        "end",
        "/电脑 end",
        "/电脑 结束会话",
        "/电脑 新会话",
        "/新会话",
    }
    _STOP_COMMANDS = {"stop", "/停止", "/电脑 stop", "/电脑 停止"}
    _STATUS_COMMANDS = {"/状态", "/电脑 状态", "status"}

    def __init__(
        self,
        harness_backend: DeepSeekHarnessBackend,
        *,
        desktop_directory: Path | None = None,
    ) -> None:
        self._harness = harness_backend
        self._desktop_files = DesktopFileRequestResolver(desktop_directory)

    async def handle_control(self, message: IncomingMessage) -> str | None:
        """Handle lifecycle commands before normal per-conversation serialization."""

        command = _normalized_command(message.content)
        if command in self._END_COMMANDS:
            interrupted, status = await self._harness.end_session(message.session_id)
            prefix = "当前任务已停止；" if interrupted else ""
            return (
                f"{prefix}当前 Agent 会话已结束，历史记录仍然保留。"
                f"下一条消息将使用全新会话 g{status.generation:04d}，不继承之前的记忆。"
            )
        if command in self._STOP_COMMANDS:
            interrupted, status = await self._harness.stop_session(message.session_id)
            if not interrupted:
                return "当前没有正在执行的 Agent 任务。"
            return (
                "当前任务已停止，未完成会话已保留为记录。"
                f"下一条消息将使用全新会话 g{status.generation:04d}。"
            )
        if command in self._STATUS_COMMANDS:
            status = self._harness.session_status(message.session_id)
            running = self._harness.is_busy(message.session_id)
            state = "正在执行任务" if running else "空闲"
            return f"统一 Agent 当前{state}，会话为 g{status.generation:04d}。"
        return None

    async def reply(self, message: IncomingMessage) -> str | AgentReply:
        """Let Harness choose between a direct answer and an available tool."""

        content = message.content.strip()
        direct_file = _strip_prefix(content, "/文件")
        if direct_file is not None:
            return _direct_file_reply(direct_file)

        desktop_file = self._desktop_files.resolve(content)
        if desktop_file is not None:
            return desktop_file

        forced_computer = _strip_prefix(content, "/电脑")
        if forced_computer is not None:
            if not forced_computer:
                return "现在不必添加 /电脑 前缀；直接描述问题或需要执行的任务即可。"
            content = forced_computer

        forced_chat = _strip_prefix(content, "/聊天")
        if forced_chat is not None:
            if not forced_chat:
                return "请在 /聊天 后写明需要回答的问题。"
            content = (
                "本条消息只允许进行知识回答，不得调用任何工具或修改电脑。\n"
                f"用户问题：{forced_chat}"
            )
        return await self._harness.reply(replace(message, content=content))

    async def close(self) -> None:
        await self._harness.close()


def _normalized_command(content: str) -> str:
    return " ".join(content.strip().split()).casefold()


def _strip_prefix(content: str, prefix: str) -> str | None:
    if content == prefix:
        return ""
    if not content.startswith(prefix):
        return None
    boundary = content[len(prefix) : len(prefix) + 1]
    if boundary not in {" ", "：", ":"}:
        return None
    return content[len(prefix) :].lstrip(" ：:")


def _direct_file_reply(raw_path: str) -> str | AgentReply:
    if not raw_path:
        return "请在 /文件 后填写要发送文件的绝对路径。"
    expanded = os.path.expandvars(raw_path.strip().strip('"'))
    try:
        path = Path(expanded).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return f"找不到这个文件：{expanded}"
    if not path.is_file():
        return "这个路径不是文件；如果要发送目录，请先让 Agent 把目录压缩成文件。"
    return AgentReply(f"已找到文件，准备发送：{path.name}", (path,))
