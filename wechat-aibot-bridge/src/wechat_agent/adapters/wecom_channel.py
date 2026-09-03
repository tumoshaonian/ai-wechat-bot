"""Enterprise WeChat intelligent-robot WebSocket adapter."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..application import MessageProcessor
from ..admin.redaction import redact_text
from ..config import Settings
from ..ports import ConversationResponder
from ..telemetry import NULL_EVENT_RECORDER, EventRecorder, safe_record
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

        await _upload_and_send_file(self._client, self._chat_id, path)


class WeComChannel:
    """Own the SDK connection and translate callbacks into application calls."""

    def __init__(
        self,
        settings: Settings,
        processor: MessageProcessor,
        *,
        event_recorder: EventRecorder = NULL_EVENT_RECORDER,
    ) -> None:
        try:
            from wecom_aibot_sdk import WSClient, generate_req_id
        except ImportError as exc:
            raise RuntimeError(
                "wecom-aibot-sdk is not installed; install the bridge package first"
            ) from exc

        self._generate_req_id = generate_req_id
        self._processor = processor
        self._event_recorder = event_recorder
        self._connection_id = settings.connection_id
        self._credential_markers = (settings.bot_id, settings.bot_secret)
        self._message_tasks: set[asyncio.Task[None]] = set()
        self._client = WSClient(settings.bot_id, settings.bot_secret)
        self._register_handlers()

    def _register_handlers(self) -> None:
        def on_authenticated() -> None:
            LOGGER.info("WeCom AI Bot authenticated")
            self._record_connection("connection.authenticated", {"state": "online"})

        def on_disconnected(reason: object = None) -> None:
            safe_reason = self._safe_sdk_message(reason)
            LOGGER.warning("WeCom AI Bot disconnected: %s", safe_reason)
            self._record_connection(
                "connection.disconnected",
                {"state": "disconnected", "reason": safe_reason},
                severity="WARNING",
            )

        def on_reconnecting(attempt: object = None) -> None:
            LOGGER.info("WeCom AI Bot reconnecting: attempt=%s", attempt)
            self._record_connection(
                "connection.reconnecting",
                {"state": "reconnecting", "attempt": str(attempt or "")},
                severity="WARNING",
            )

        def on_error(error: object) -> None:
            safe_error = self._safe_sdk_message(error)
            error_code = str(getattr(error, "code", "") or "SDK_ERROR").upper()
            error_text = f"{type(error).__name__} {error}".lower()
            authentication_failure = "AUTH" in error_code or any(
                marker in error_text
                for marker in ("authentication", "authenticate", "credential", "unauthorized")
            )
            phase = "authentication" if authentication_failure else "runtime"
            if authentication_failure and error_code == "SDK_ERROR":
                error_code = "AUTHENTICATION_FAILED"
            LOGGER.error(
                "WeCom AI Bot SDK error phase=%s code=%s message=%s",
                phase,
                error_code,
                safe_error,
            )
            self._record_connection(
                "connection.authentication_failed"
                if authentication_failure
                else "connection.error",
                {
                    "state": "failed" if authentication_failure else "degraded",
                    "phase": phase,
                    "code": error_code,
                    "error": safe_error,
                },
                severity="ERROR",
            )

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
            message = replace(
                parse_text_message(frame),
                connection_id=self._connection_id,
            )
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

        self._record_connection("connection.connecting", {"state": "connecting"})
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
            self._record_connection("connection.stopped", {"state": "stopped"})

    async def send_file_to_chat(
        self,
        path: Path,
        chat_id: str,
        *,
        connection_id: str,
    ) -> dict[str, object]:
        """Deliver an existing artifact for an authenticated admin retry."""

        if connection_id != self._connection_id:
            raise RuntimeError("The requested WeCom connection is not active in this worker")
        size = await _upload_and_send_file(self._client, chat_id, path)
        return {"sent": True, "size_bytes": size}

    def _record_connection(
        self,
        event_type: str,
        payload: Mapping[str, object],
        *,
        severity: str = "INFO",
    ) -> None:
        safe_record(
            self._event_recorder,
            event_type,
            actor_type="wecom_connection",
            actor_id=self._connection_id,
            resource_type="connection",
            resource_id=self._connection_id,
            payload={"connection_id": self._connection_id, **payload},
            severity=severity,
        )

    def _safe_sdk_message(self, value: object) -> str:
        text = str(value or "")
        for marker in sorted(
            {item for item in self._credential_markers if item},
            key=len,
            reverse=True,
        ):
            text = text.replace(marker, "***")
        return redact_text(text, max_length=1000)


async def _upload_and_send_file(client: Any, chat_id: str, path: Path) -> int:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"not a file: {resolved}")
    size = resolved.stat().st_size
    if size <= 0:
        raise ValueError(f"cannot send an empty file: {resolved}")
    if size > WeComStreamResponder.MAX_FILE_BYTES:
        raise ValueError(
            f"file exceeds the 50 MiB WeCom SDK limit: {resolved} ({size} bytes)"
        )
    data = await asyncio.to_thread(resolved.read_bytes)
    uploaded = await client.upload_media(data, type="file", filename=resolved.name)
    media_id = uploaded.get("media_id")
    if not media_id:
        raise RuntimeError(f"WeCom upload returned no media_id for {resolved}")
    await client.send_media_message(chat_id, "file", media_id)
    LOGGER.info(
        "Delivered enterprise WeChat file chat_id=%s path=%s size=%s",
        chat_id,
        resolved,
        size,
    )
    return size
