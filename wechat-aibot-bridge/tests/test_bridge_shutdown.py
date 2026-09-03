"""Bridge supervisor shutdown handshake tests."""

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from wechat_agent.__main__ import _run_channel_until_shutdown


class _BlockingChannel:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def run(self) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class BridgeShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_request_cancels_channel_and_is_consumed(self) -> None:
        with TemporaryDirectory() as temporary:
            request = Path(temporary) / "runtime" / "bridge.stop.request"
            channel = _BlockingChannel()
            running = asyncio.create_task(
                _run_channel_until_shutdown(channel, request)  # type: ignore[arg-type]
            )
            await channel.started.wait()

            request.write_text("stop", encoding="utf-8")
            await asyncio.wait_for(running, timeout=2)

            self.assertTrue(channel.cancelled)
            self.assertFalse(request.exists())


if __name__ == "__main__":
    unittest.main()
