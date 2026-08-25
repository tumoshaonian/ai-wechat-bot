"""Pure conversion from enterprise WeChat payloads to domain messages."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..domain import IncomingMessage


class InvalidWeComPayload(ValueError):
    """Raised when a callback lacks required message fields."""


def parse_text_message(frame: Mapping[str, Any]) -> IncomingMessage:
    """Convert a text WebSocket frame into a normalized message."""

    body = _mapping(frame.get("body"), "body")
    sender = _mapping(body.get("from"), "body.from")
    text = _mapping(body.get("text"), "body.text")

    sender_id = _text(sender.get("userid"), "body.from.userid")
    content = _text(text.get("content"), "body.text.content")
    message_id = str(body.get("msgid") or "").strip()
    raw_chat_type = str(body.get("chattype") or "single").strip().lower()
    if raw_chat_type not in {"single", "group"}:
        raise InvalidWeComPayload(f"unsupported chattype: {raw_chat_type}")
    chat_id = str(body.get("chatid") or sender_id).strip()
    if not chat_id:
        raise InvalidWeComPayload("body.chatid is required for a group message")

    return IncomingMessage(
        message_id=message_id,
        sender_id=sender_id,
        chat_id=chat_id,
        chat_type=raw_chat_type,
        content=content,
    )


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InvalidWeComPayload(f"{field_name} must be an object")
    return value


def _text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise InvalidWeComPayload(f"{field_name} must not be empty")
    return text
