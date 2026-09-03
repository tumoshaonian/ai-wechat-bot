"""Secure local management control plane for the WeCom Agent.

The event recorder intentionally has no FastAPI dependency, so the Bridge can keep
processing messages even when the HTTP control-plane extras are unavailable.
"""

from .events import AdminEventRecorder, get_event_recorder


def create_app(*args, **kwargs):
    from .api import create_app as factory

    return factory(*args, **kwargs)


__all__ = ["AdminEventRecorder", "create_app", "get_event_recorder"]
