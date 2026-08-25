"""Async adapter for the existing Spring Boot chat endpoint."""

from __future__ import annotations

import httpx

from ..domain import IncomingMessage


class ChatBackendError(RuntimeError):
    """Raised when the Spring chat service cannot produce a valid reply."""


class SpringChatBackend:
    """Forward normalized messages to `/api/wechat/reply`."""

    def __init__(self, url: str, *, timeout_seconds: float) -> None:
        self._url = url
        self._client = httpx.AsyncClient(timeout=timeout_seconds)

    async def reply(self, message: IncomingMessage) -> str:
        """Request one assistant reply from Spring Boot."""

        payload = {
            "message": message.content,
            "sessionId": message.session_id,
            "fromUser": message.sender_id,
            "chatName": message.chat_id,
            "groupChat": message.is_group,
            "source": "wecom-aibot-sdk",
        }
        try:
            response = await self._client.post(self._url, json=payload)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ChatBackendError(f"Spring chat request failed: {exc}") from exc

        reply = str(body.get("reply") or "").strip() if isinstance(body, dict) else ""
        if not reply:
            raise ChatBackendError("Spring chat response did not contain a reply")
        return reply

    async def close(self) -> None:
        """Close the shared HTTP connection pool."""

        await self._client.aclose()
