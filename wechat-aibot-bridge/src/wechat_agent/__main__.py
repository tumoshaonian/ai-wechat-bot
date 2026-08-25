"""Process entry point and dependency composition root."""

from __future__ import annotations

import asyncio
import logging

from .adapters.deepseek_harness import DeepSeekHarnessBackend
from .adapters.spring_chat import SpringChatBackend
from .adapters.unified_agent import UnifiedAgentBackend
from .adapters.wecom_channel import WeComChannel
from .application import MessageProcessor
from .config import ConfigurationError, Settings


LOGGER = logging.getLogger(__name__)


async def serve(settings: Settings) -> None:
    """Assemble adapters and run the long-connection service."""

    if settings.harness_enabled:
        backend = UnifiedAgentBackend(DeepSeekHarnessBackend(settings))
        LOGGER.info(
            "Unified DeepSeek Harness Agent enabled workspace=%s; /电脑 is optional",
            settings.harness_workspace,
        )
    else:
        backend = SpringChatBackend(
            settings.spring_boot_url,
            timeout_seconds=settings.request_timeout_seconds,
        )
        LOGGER.warning("Legacy Spring chat mode enabled because HARNESS_ENABLED=false")
    processor = MessageProcessor(
        backend,
        allowed_user_ids=settings.allowed_user_ids,
        progress_interval_seconds=settings.progress_interval_seconds,
    )
    channel = WeComChannel(settings, processor)
    try:
        await channel.run()
    finally:
        await processor.close()


def main() -> None:
    """Load configuration and own the process event loop."""

    try:
        settings = Settings.from_environment()
    except ConfigurationError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    LOGGER.info("Starting enterprise WeChat AI Bot bridge")
    try:
        asyncio.run(serve(settings))
    except KeyboardInterrupt:
        LOGGER.info("Enterprise WeChat AI Bot bridge stopped")


if __name__ == "__main__":
    main()
