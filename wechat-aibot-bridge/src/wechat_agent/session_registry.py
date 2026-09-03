"""Durable conversation generations for Harness-backed agent sessions."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class HarnessSessionLease:
    """One execution lease bound to a concrete Harness session generation."""

    chat_key: str
    generation: int
    session_id: str
    recovered_interrupted_session: bool = False


@dataclass(frozen=True, slots=True)
class HarnessConversationStatus:
    """User-facing status without exposing the original WeCom conversation ID."""

    generation: int
    state: str


class HarnessSessionRegistry:
    """Persist the current Harness generation separately from Harness event logs."""

    STATE_VERSION = 2

    def __init__(self, session_root: Path) -> None:
        self._path = session_root / "bridge-conversations.json"
        self._lock = threading.RLock()

    def begin(self, chat_session_id: str) -> HarnessSessionLease:
        """Mark a generation running, rotating first after an unclean shutdown."""

        chat_key = _chat_key(chat_session_id)
        with self._lock:
            state = self._load()
            conversations = state.setdefault("conversations", {})
            record_exists = isinstance(conversations.get(chat_key), dict)
            record = _record(state, chat_key)
            generation = _generation(record)
            recovered = record.get("state") == "running"
            migrated = record_exists and state.get("version") != self.STATE_VERSION
            if recovered:
                generation += 1
            state["version"] = self.STATE_VERSION
            state["conversations"][chat_key] = {
                "generation": generation,
                "state": "running",
                "updatedAt": _now(),
                "previousState": (
                    "interrupted"
                    if recovered
                    else "stable-session-id-migration"
                    if migrated
                    else record.get("previousState")
                ),
            }
            self._save(state)
        return HarnessSessionLease(
            chat_key=chat_key,
            generation=generation,
            session_id=_harness_session_id(chat_key, generation),
            recovered_interrupted_session=recovered,
        )

    def finish(self, lease: HarnessSessionLease, *, state_name: str = "idle") -> None:
        """Close a lease only if it is still the current generation."""

        with self._lock:
            state = self._load()
            record = _record(state, lease.chat_key)
            if _generation(record) != lease.generation:
                return
            record.update({"state": state_name, "updatedAt": _now()})
            state["conversations"][lease.chat_key] = record
            self._save(state)

    def rotate(self, chat_session_id: str, *, reason: str) -> HarnessConversationStatus:
        """End the current generation while keeping its Harness log on disk."""

        chat_key = _chat_key(chat_session_id)
        with self._lock:
            state = self._load()
            record = _record(state, chat_key)
            generation = _generation(record) + 1
            state["conversations"][chat_key] = {
                "generation": generation,
                "state": "idle",
                "updatedAt": _now(),
                "previousState": reason,
            }
            state["version"] = self.STATE_VERSION
            self._save(state)
        return HarnessConversationStatus(generation=generation, state="idle")

    def status(self, chat_session_id: str) -> HarnessConversationStatus:
        chat_key = _chat_key(chat_session_id)
        with self._lock:
            record = _record(self._load(), chat_key)
            return HarnessConversationStatus(
                generation=_generation(record),
                state=str(record.get("state") or "idle"),
            )

    def _load(self) -> dict[str, Any]:
        if not self._path.is_file():
            return {"version": self.STATE_VERSION, "conversations": {}}
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": self.STATE_VERSION, "conversations": {}}
        conversations = value.get("conversations") if isinstance(value, dict) else None
        if not isinstance(conversations, dict):
            conversations = {}
        version = value.get("version") if isinstance(value, dict) else None
        return {
            "version": version if isinstance(version, int) else 1,
            "conversations": conversations,
        }

    def _save(self, state: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self._path)


def _record(state: dict[str, Any], chat_key: str) -> dict[str, Any]:
    conversations = state.setdefault("conversations", {})
    value = conversations.get(chat_key)
    return dict(value) if isinstance(value, dict) else {"generation": 1, "state": "idle"}


def _generation(record: dict[str, Any]) -> int:
    value = record.get("generation")
    return value if isinstance(value, int) and value > 0 else 1


def _chat_key(chat_session_id: str) -> str:
    return hashlib.sha256(chat_session_id.encode("utf-8")).hexdigest()[:24]


def _harness_session_id(chat_key: str, generation: int) -> str:
    """Return a durable id that survives clean Bridge/Runtime restarts."""

    return f"wecom-{chat_key}-g{generation:04d}"


def _now() -> str:
    return datetime.now(UTC).isoformat()
