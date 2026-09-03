"""CLI entry point for the independent local administration API."""

from __future__ import annotations

import logging

import uvicorn

from .api import create_app
from .config import AdminSettings


def main() -> None:
    settings = AdminSettings.from_environment()
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        access_log=True,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
