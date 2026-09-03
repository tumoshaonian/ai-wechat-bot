"""Central redaction for logs, events, audit records and API payloads."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


_SENSITIVE_KEY = re.compile(
    r"(?:secret|password|passwd|api[_-]?key|authorization|cookie|token|credential)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_ASSIGNMENT = re.compile(
    r"(?i)\b(secret|password|passwd|api[_-]?key|token)\b(\s*[:=]\s*)[^\s,;]+"
)


def redact_text(value: str, *, max_length: int = 16_384) -> str:
    text = _BEARER.sub(r"\1***", value)
    text = _ASSIGNMENT.sub(r"\1\2***", text)
    if len(text) > max_length:
        return text[:max_length] + f"…[truncated {len(text) - max_length} chars]"
    return text


def redact_data(value: Any, *, max_depth: int = 8) -> Any:
    if max_depth <= 0:
        return "[max-depth]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            result[key_text] = (
                "***"
                if _SENSITIVE_KEY.search(key_text)
                else redact_data(item, max_depth=max_depth - 1)
            )
        return result
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [redact_data(item, max_depth=max_depth - 1) for item in value[:500]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(str(value))
