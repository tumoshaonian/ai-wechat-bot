"""Process entry point and dependency composition root."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .adapters.deepseek_harness import DeepSeekHarnessBackend
from .adapters.spring_chat import SpringChatBackend
from .adapters.unified_agent import UnifiedAgentBackend
from .adapters.wecom_channel import WeComChannel
from .admin.events import get_event_recorder
from .admin.logging_handler import install_database_log_handler
from .application import MessageProcessor
from .config import ConfigurationError, Settings
from .control_worker import AdminControlWorker
from .telemetry import safe_record


LOGGER = logging.getLogger(__name__)


async def serve(settings: Settings) -> None:
    """Assemble adapters and run the long-connection service."""

    event_recorder = get_event_recorder()
    ensure_connection = getattr(event_recorder, "ensure_environment_connection", None)
    if callable(ensure_connection):
        await asyncio.to_thread(
            ensure_connection,
            settings.connection_id,
            settings.bot_id,
            settings.bot_secret,
        )
    store = getattr(event_recorder, "store", None)
    reconcile = getattr(store, "reconcile_interrupted_tasks", None)
    if callable(reconcile):
        interrupted = await asyncio.to_thread(reconcile)
        if interrupted:
            LOGGER.warning(
                "Reconciled %s tasks interrupted by a previous Bridge process",
                interrupted,
            )
    safe_record(
        event_recorder,
        "runtime.bridge.started",
        actor_type="service",
        actor_id="bridge",
        resource_type="service",
        resource_id="bridge",
        payload={"service": "bridge", "state": "running"},
    )
    if settings.harness_enabled:
        harness_backend = DeepSeekHarnessBackend(
            settings,
            event_recorder=event_recorder,
        )
        LOGGER.info(
            "Initializing DeepSeek Harness profile=%s runtime_mode=%s home=%s",
            settings.harness_profile,
            settings.harness_runtime_mode,
            settings.harness_dsh_home,
        )
        await harness_backend.start()
        backend = UnifiedAgentBackend(
            harness_backend
        )
        LOGGER.info(
            "Unified DeepSeek Harness Agent ready workspace=%s; /电脑 is optional",
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
        task_timeout_seconds=settings.task_timeout_seconds,
        event_recorder=event_recorder,
    )
    channel = WeComChannel(
        settings,
        processor,
        event_recorder=event_recorder,
    )
    control_worker = AdminControlWorker(
        event_recorder,
        backend,
        file_sender=channel,
    )
    await control_worker.start()
    try:
        await _run_channel_until_shutdown(channel, settings.bridge_shutdown_file)
    finally:
        await control_worker.close()
        await processor.close()
        safe_record(
            event_recorder,
            "runtime.bridge.stopped",
            actor_type="service",
            actor_id="bridge",
            resource_type="service",
            resource_id="bridge",
            payload={"service": "bridge", "state": "stopped"},
        )


async def _run_channel_until_shutdown(
    channel: WeComChannel,
    shutdown_file: Path,
) -> None:
    """Run the channel until it exits or the local supervisor requests a stop."""

    shutdown_file.parent.mkdir(parents=True, exist_ok=True)
    shutdown_file.unlink(missing_ok=True)
    channel_task = asyncio.create_task(channel.run(), name="wecom-channel")
    shutdown_task = asyncio.create_task(
        _wait_for_shutdown_file(shutdown_file),
        name="bridge-shutdown-request",
    )
    try:
        done, _pending = await asyncio.wait(
            (channel_task, shutdown_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if channel_task in done:
            await channel_task
            return
        LOGGER.info("Bridge shutdown requested through %s", shutdown_file)
        channel_task.cancel()
        await asyncio.gather(channel_task, return_exceptions=True)
    finally:
        shutdown_task.cancel()
        await asyncio.gather(shutdown_task, return_exceptions=True)
        shutdown_file.unlink(missing_ok=True)


async def _wait_for_shutdown_file(shutdown_file: Path) -> None:
    """Wait asynchronously for the supervisor's exact shutdown request file."""

    while not shutdown_file.is_file():
        await asyncio.sleep(0.25)


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
    install_database_log_handler(service="bridge")
    LOGGER.info("Starting enterprise WeChat AI Bot bridge")
    try:
        asyncio.run(serve(settings))
    except KeyboardInterrupt:
        LOGGER.info("Enterprise WeChat AI Bot bridge stopped")


if __name__ == "__main__":
    main()
