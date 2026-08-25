"""Enterprise WeChat intelligent-robot WebSocket adapter."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..application import MessageProcessor
from ..config import Settings
from ..ports import ConversationResponder
from .wecom_payload import InvalidWeComPayload, parse_text_message


LOGGER = logging.getLogger(__name__)


class WeComStreamResponder(ConversationResponder):
    """Reply to one callback frame through a single streaming message."""

    MAX_FILE_BYTES = 50 * 1024 * 1024

    def __init__(
        self,
        client: Any,
        frame: Mapping[str, Any],
        stream_id: str,
        chat_id: str,
    ) -> None:
        self._client = client
        self._frame = frame
        self._stream_id = stream_id
        self._chat_id = chat_id

    async def send(self, text: str, *, finish: bool) -> None:
        """Publish one stream update for the callback frame."""

        await self._client.reply_stream(self._frame, self._stream_id, text, finish)

    async def send_file(self, path: Path) -> None:
        """Upload one local file as temporary media, then push it to this chat."""

        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise ValueError(f"not a file: {resolved}")
        size = resolved.stat().st_size
        if size <= 0:
            raise ValueError(f"cannot send an empty file: {resolved}")
        if size > self.MAX_FILE_BYTES:
            raise ValueError(
                f"file exceeds the 50 MiB WeCom SDK limit: {resolved} ({size} bytes)"
            )
        data = await asyncio.to_thread(resolved.read_bytes)
        uploaded = await self._client.upload_media(
            data,
            type="file",
            filename=resolved.name,
        )
        media_id = uploaded.get("media_id")
        if not media_id:
            raise RuntimeError(f"WeCom upload returned no media_id for {resolved}")
        await self._client.send_media_message(self._chat_id, "file", media_id)
        LOGGER.info(
            "Delivered enterprise WeChat file chat_id=%s path=%s size=%s",
            self._chat_id,
            resolved,
            size,
        )


class WeComChannel:
    """Own the SDK connection and translate callbacks into application calls."""

    def __init__(self, settings: Settings, processor: MessageProcessor) -> None:
        try:
            from wecom_aibot_sdk import WSClient, generate_req_id
        except ImportError as exc:
            raise RuntimeError(
                "wecom-aibot-sdk is not installed; install the bridge package first"
            ) from exc

        self._generate_req_id = generate_req_id
        self._processor = processor
        self._message_tasks: set[asyncio.Task[None]] = set()
        self._client = WSClient(settings.bot_id, settings.bot_secret)
        self._register_handlers()

    def _register_handlers(self) -> None:
        def on_authenticated() -> None:
            LOGGER.info("WeCom AI Bot authenticated")

        def on_disconnected(reason: object = None) -> None:
            LOGGER.warning("WeCom AI Bot disconnected: %s", reason)

        def on_reconnecting(attempt: object = None) -> None:
            LOGGER.info("WeCom AI Bot reconnecting: attempt=%s", attempt)

        def on_error(error: object) -> None:
            LOGGER.error("WeCom AI Bot SDK error: %s", error)

        def on_text(frame: Mapping[str, Any]) -> None:
            task = asyncio.create_task(self._handle_text(frame))
            self._message_tasks.add(task)
            task.add_done_callback(self._message_tasks.discard)

        self._client.on("authenticated", on_authenticated)
        self._client.on("disconnected", on_disconnected)
        self._client.on("reconnecting", on_reconnecting)
        self._client.on("error", on_error)
        self._client.on("message.text", on_text)

    async def _handle_text(self, frame: Mapping[str, Any]) -> None:
        """Process a frame independently so control messages can interrupt a task."""

        try:
            message = parse_text_message(frame)
        except InvalidWeComPayload:
            LOGGER.exception("Ignored invalid enterprise WeChat text payload")
            return

        LOGGER.info(
            "Received enterprise WeChat message: message_id=%s sender=%s chat_type=%s",
            message.message_id,
            message.sender_id,
            message.chat_type,
        )
        responder = WeComStreamResponder(
            self._client,
            frame,
            self._generate_req_id("stream"),
            message.chat_id,
        )
        try:
            await self._processor.handle(message, responder)
        except Exception:
            LOGGER.exception("Unhandled enterprise WeChat message task failure")

    async def run(self) -> None:
        """Connect and remain alive until the process is cancelled."""

        await self._client.connect()
        try:
            await asyncio.Event().wait()
        finally:
            tasks = list(self._message_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self._client.disconnect()
