"""Interfaces owned by the application layer."""

from pathlib import Path
from typing import Protocol

from .domain import AgentReply, IncomingMessage


class ChatBackend(Protocol):
    """Generate a response for one normalized chat message."""

    async def reply(self, message: IncomingMessage) -> str | AgentReply:
        """Return the final assistant text for the message."""

    async def close(self) -> None:
        """Release backend resources."""


class ConversationResponder(Protocol):
    """Publish progress and final text to the originating conversation."""

    async def send(self, text: str, *, finish: bool) -> None:
        """Publish one stream update, marking the last update with finish."""

    async def send_file(self, path: Path) -> None:
        """Upload and deliver one local file to the originating conversation."""
