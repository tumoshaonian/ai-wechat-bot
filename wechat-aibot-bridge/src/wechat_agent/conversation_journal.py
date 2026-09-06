"""Durable channel facts, independent of disposable SDK runtime identities.

This is explicit transcript reconstruction, not native Harness event-log resume.
Oversized recovery fails closed instead of silently losing the oldest facts.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from contextlib import contextmanager
from typing import Iterator

from .domain import UserVisibleError


class ConversationJournal:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "channel-history.sqlite3"
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS epochs(chat TEXT PRIMARY KEY, epoch INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS facts(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, chat TEXT NOT NULL,
                    epoch INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE INDEX IF NOT EXISTS facts_chat_epoch ON facts(chat, epoch, seq);
                CREATE TABLE IF NOT EXISTS deliveries(
                    task TEXT NOT NULL, artifact TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(task,artifact));
            """)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def epoch(self, chat: str) -> int:
        with self._db() as db:
            row = db.execute("SELECT epoch FROM epochs WHERE chat=?", (chat,)).fetchone()
        return int(row[0]) if row else 1

    def claim_delivery(self, task: str, artifact: str, initial: dict) -> dict | None:
        with self._db() as db:
            cursor = db.execute("INSERT OR IGNORE INTO deliveries VALUES (?,?,?)", (task, artifact, json.dumps(initial, ensure_ascii=False)))
            if cursor.rowcount == 1:
                return None
            return json.loads(db.execute("SELECT payload FROM deliveries WHERE task=? AND artifact=?", (task, artifact)).fetchone()[0])

    def finish_delivery(self, task: str, artifact: str, result: dict) -> None:
        with self._db() as db:
            db.execute("UPDATE deliveries SET payload=? WHERE task=? AND artifact=?", (json.dumps(result, ensure_ascii=False), task, artifact))

    def end(self, chat: str) -> None:
        with self._db() as db:
            db.execute("INSERT INTO epochs VALUES (?,2) ON CONFLICT(chat) DO UPDATE SET epoch=epoch+1", (chat,))

    def append(self, chat: str, epoch: int, kind: str, payload: object) -> int:
        with self._db() as db:
            cursor = db.execute(
                "INSERT INTO facts(chat,epoch,kind,payload) VALUES (?,?,?,?)",
                (chat, epoch, kind, json.dumps(payload, ensure_ascii=False)),
            )
            return int(cursor.lastrowid)

    def context(self, chat: str, epoch: int, after: int, max_bytes: int) -> tuple[str, int]:
        entries = []
        last = after
        size = 0
        with self._db() as db:
            for seq, kind, payload in db.execute(
                "SELECT seq,kind,payload FROM facts WHERE chat=? AND epoch=? AND seq>? ORDER BY seq",
                (chat, epoch, after),
            ):
                # Preserve whole entries; never split a request or its outcome.
                entry = json.dumps({"kind": kind, "data": json.loads(payload)}, ensure_ascii=False)
                size += len(entry.encode("utf-8")) + 1
                if size > max_bytes:
                    raise UserVisibleError(
                        "当前历史超过安全恢复预算，未截断或删除记录，也未执行本次操作。"
                        "请使用 end 开启新会话，或由管理员调整恢复预算。",
                        code="HISTORY_RECOVERY_BUDGET",
                    )
                entries.append(entry)
                last = seq
        return "\n".join(entries), last
