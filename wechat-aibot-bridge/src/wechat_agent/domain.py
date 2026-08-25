"""Domain types shared by message channels and agent backends."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


ChatType = Literal["single", "group"]


class AgentTaskInterrupted(RuntimeError):
    """Raised when the user intentionally stops an active agent task."""


class UserVisibleError(RuntimeError):
    """A backend failure whose safe explanation should be shown to the user."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.user_message = message
        self.code = code


@dataclass(frozen=True, slots=True)
class AgentReply:
    """One final agent answer plus local files to deliver through the channel."""

    text: str
    files: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """A normalized user message independent of the transport SDK."""

    message_id: str
    sender_id: str
    chat_id: str
    chat_type: ChatType
    content: str

    @property
    def session_id(self) -> str:
        """Return the stable backend conversation identifier."""

        return f"wecom:{self.chat_type}:{self.chat_id}"

    @property
    def is_group(self) -> bool:
        """Return whether the message came from a group conversation."""

        return self.chat_type == "group"
