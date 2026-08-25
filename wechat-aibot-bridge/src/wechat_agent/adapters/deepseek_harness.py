"""DeepSeek Harness computer-operation backend."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

from ..domain import AgentReply, AgentTaskInterrupted, IncomingMessage, UserVisibleError
from ..ports import ChatBackend
from ..session_registry import HarnessConversationStatus, HarnessSessionLease, HarnessSessionRegistry

if TYPE_CHECKING:
    from ..config import Settings


LOGGER = logging.getLogger(__name__)
FILE_TAG_PATTERN = re.compile(r"<wechat-file>\s*(.*?)\s*</wechat-file>", re.IGNORECASE | re.DOTALL)


class HarnessLike(Protocol):
    """The small synchronous SDK surface used by this adapter."""

    def run(self, input: str, *, session_id: str) -> Any:
        """Run one agent turn."""

    def close(self) -> None:
        """Stop the owned runtime process."""


HarnessFactory = Callable[["Settings"], HarnessLike]


class DeepSeekHarnessBackend(ChatBackend):
    """Reuse one local Harness runtime and preserve one session per chat."""

    def __init__(
        self,
        settings: Settings,
        *,
        harness_factory: HarnessFactory | None = None,
    ) -> None:
        self._settings = settings
        self._harness_factory = harness_factory or _create_harness
        self._harness: HarnessLike | None = None
        self._operation_lock = asyncio.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dsh-runtime")
        self._runtime_guard = threading.RLock()
        self._registry = HarnessSessionRegistry(settings.harness_session_root)
        self._active_chat_session_id: str | None = None
        self._interrupt_requested_for: set[str] = set()
        self._closed = False

    async def reply(self, message: IncomingMessage) -> AgentReply:
        if self._closed:
            raise RuntimeError("DeepSeek Harness backend is closed")

        async with self._operation_lock:
            lease = self._registry.begin(message.session_id)
            if lease.recovered_interrupted_session:
                LOGGER.warning(
                    "Rotated an unclean Harness session before processing chat_session=%s generation=%s",
                    message.session_id,
                    lease.generation,
                )
            with self._runtime_guard:
                self._active_chat_session_id = message.session_id
            try:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    self._executor,
                    self._run_sync,
                    message.content,
                    lease.session_id,
                )
            except Exception as exc:
                if self._consume_interrupt(message.session_id):
                    raise AgentTaskInterrupted("DeepSeek Harness task was stopped") from exc
                self._registry.rotate(message.session_id, reason="runtime-error")
                raise
            finally:
                with self._runtime_guard:
                    if self._active_chat_session_id == message.session_id:
                        self._active_chat_session_id = None
                self._registry.finish(lease)

        finish_reason = getattr(result, "finish_reason", None)
        if finish_reason == "error":
            detail, code = _harness_error_detail(result)
            suffix = f" [{code}]" if code else ""
            LOGGER.error("DeepSeek Harness finished with an error%s: %s", suffix, detail)
            self._registry.rotate(message.session_id, reason=f"agent-error:{code or 'unknown'}")
            raise UserVisibleError(_friendly_harness_error(detail, code), code=code)
        final_response = str(getattr(result, "final_response", "") or "").strip()
        if not final_response:
            raise RuntimeError("DeepSeek Harness returned an empty response")
        reply = _extract_file_deliveries(final_response)
        LOGGER.info(
            "DeepSeek Harness completed session=%s finish_reason=%s files=%s",
            message.session_id,
            finish_reason,
            len(reply.files),
        )
        return reply

    def _run_sync(self, content: str, session_id: str) -> Any:
        with self._runtime_guard:
            if self._harness is None:
                self._harness = self._harness_factory(self._settings)
            harness = self._harness
        return harness.run(content, session_id=session_id)

    def is_busy(self, chat_session_id: str) -> bool:
        with self._runtime_guard:
            return self._active_chat_session_id == chat_session_id

    def session_status(self, chat_session_id: str) -> HarnessConversationStatus:
        return self._registry.status(chat_session_id)

    async def stop_session(
        self,
        chat_session_id: str,
    ) -> tuple[bool, HarnessConversationStatus]:
        interrupted = await self.interrupt_session(chat_session_id)
        status = self._registry.rotate(chat_session_id, reason="stopped") if interrupted else self._registry.status(chat_session_id)
        return interrupted, status

    async def end_session(
        self,
        chat_session_id: str,
    ) -> tuple[bool, HarnessConversationStatus]:
        interrupted = await self.interrupt_session(chat_session_id)
        return interrupted, self._registry.rotate(chat_session_id, reason="ended")

    async def interrupt_session(self, chat_session_id: str) -> bool:
        with self._runtime_guard:
            if self._active_chat_session_id != chat_session_id:
                return False
            self._interrupt_requested_for.add(chat_session_id)
        await asyncio.to_thread(self._close_runtime_sync)
        return True

    def _consume_interrupt(self, chat_session_id: str) -> bool:
        with self._runtime_guard:
            if chat_session_id not in self._interrupt_requested_for:
                return False
            self._interrupt_requested_for.discard(chat_session_id)
            return True

    def _close_runtime_sync(self) -> None:
        with self._runtime_guard:
            harness = self._harness
            self._harness = None
        if harness is not None:
            harness.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._close_runtime_sync)
        await asyncio.to_thread(self._executor.shutdown, True, cancel_futures=True)


def _harness_error_detail(result: Any) -> tuple[str, str | None]:
    events = getattr(result, "events", None)
    if isinstance(events, list):
        for event in reversed(events):
            if not isinstance(event, dict) or event.get("type") != "turn/end":
                continue
            data = event.get("data")
            reason = data.get("reason") if isinstance(data, dict) else None
            if not isinstance(reason, dict):
                continue
            failure = reason.get("error") or reason.get("failure")
            if isinstance(failure, dict):
                message = str(failure.get("message") or "unknown Harness error")
                code = failure.get("code")
                return message, str(code) if code else None
    return "unknown Harness error", None


def _friendly_harness_error(detail: str, code: str | None) -> str:
    normalized = (code or "").upper()
    if normalized == "QUOTA":
        return (
            "DeepSeek API 余额不足，Agent 暂时无法继续执行。请为当前 API Key 充值，"
            "或在电脑端更换有余额的模型/API Key 后重试；本次失败会话已自动隔离。"
        )
    if normalized in {"AUTH", "AUTHENTICATION", "UNAUTHORIZED"}:
        return "DeepSeek API Key 无效或没有权限，请检查电脑端 .env 配置后重试。"
    if normalized == "RATE_LIMIT":
        return "DeepSeek API 当前触发限流，请稍后再试；本次失败会话已自动隔离。"
    return f"Agent 执行失败（{code or 'UNKNOWN'}）：{detail}。本次失败会话已自动隔离。"


def _extract_file_deliveries(response: str) -> AgentReply:
    """Parse Harness-to-bridge file handoff tags and validate local paths."""

    paths: list[Path] = []
    errors: list[str] = []
    seen: set[Path] = set()
    for match in FILE_TAG_PATTERN.finditer(response):
        raw_path = os.path.expandvars(match.group(1).strip().strip('"'))
        try:
            path = Path(raw_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            errors.append(f"找不到待发送文件：{raw_path}")
            continue
        if not path.is_file():
            errors.append(f"待发送路径不是文件：{path}")
            continue
        if path in seen:
            continue
        seen.add(path)
        paths.append(path)

    clean_text = FILE_TAG_PATTERN.sub("", response).strip()
    if errors:
        clean_text = "\n\n".join(part for part in (clean_text, *errors) if part)
    return AgentReply(clean_text, tuple(paths))


def _create_harness(settings: Settings) -> HarnessLike:
    try:
        from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig
    except ImportError as exc:
        raise RuntimeError(
            "deepseek-harness-sdk is not installed; follow the local SDK install command in README"
        ) from exc

    settings.harness_session_root.mkdir(parents=True, exist_ok=True)
    config = DeepSeekHarnessConfig(
        provider=settings.harness_provider,
        model=settings.harness_model,
        max_tokens=settings.harness_max_tokens,
        cwd=str(settings.harness_workspace),
        runtime_cwd=str(settings.harness_repo_path),
        session_root=str(settings.harness_session_root),
        cordis=str(settings.harness_cordis_config),
        env={"DSH_SYSTEM_PROMPT": settings.harness_system_prompt},
        launch_args_override=(
            settings.harness_node_bin,
            str(settings.harness_runtime_bin_js),
        ),
        request_timeout_seconds=settings.harness_request_timeout_seconds,
    )
    return DeepSeekHarness(config)
