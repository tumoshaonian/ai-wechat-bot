"""Deterministic Windows desktop automation exposed to DeepSeek Harness."""

from .worker import DesktopWorker, DesktopWorkerError

__all__ = ["DesktopWorker", "DesktopWorkerError"]
