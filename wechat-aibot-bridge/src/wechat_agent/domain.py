"""Domain types shared by message channels and agent backends."""

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote
from typing import Literal, Mapping


ChatType = Literal["single", "group"]


def conversation_key(connection_id: str, chat_type: str, chat_id: str) -> str:
    return f"wecom:{quote(connection_id, safe='')}:{chat_type}:{quote(chat_id, safe='')}"


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
    status: str = "succeeded"


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """A normalized user message independent of the transport SDK."""

    message_id: str
    sender_id: str
    chat_id: str
    chat_type: ChatType
    content: str
    connection_id: str = "default"
    task_id: str | None = None
    trace_id: str | None = None
    access_policy: Mapping[str, object] = field(default_factory=dict)

    @property
    def session_id(self) -> str:
        """Return the stable backend conversation identifier."""

        return conversation_key(self.connection_id, self.chat_type, self.chat_id)

    @property
    def is_group(self) -> bool:
        """Return whether the message came from a group conversation."""

        return self.chat_type == "group"
