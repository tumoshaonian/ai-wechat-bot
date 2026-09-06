"""Durable channel facts, independent of disposable SDK runtime identities.

This is explicit transcript reconstruction, not native Harness event-log resume.
Oversized recovery requires explicit summary checkpoints; original facts remain.
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
                CREATE TABLE IF NOT EXISTS checkpoints(
                    chat TEXT NOT NULL, epoch INTEGER NOT NULL, through_seq INTEGER NOT NULL,
                    summary TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(chat,epoch,through_seq));
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
            checkpoint = db.execute(
                "SELECT through_seq,summary FROM checkpoints WHERE chat=? AND epoch=? ORDER BY through_seq DESC LIMIT 1", (chat, epoch),
            ).fetchone()
            if checkpoint and checkpoint[0] > after:
                entry = json.dumps({"kind": "history_summary", "through_seq": checkpoint[0], "data": checkpoint[1]}, ensure_ascii=False)
                size = len(entry.encode("utf-8")) + 1
                if size > max_bytes:
                    raise UserVisibleError("历史摘要超过恢复预算，未执行本次操作。", code="HISTORY_RECOVERY_BUDGET")
                entries.append(entry)
                after = last = checkpoint[0]
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

    def summary_batch(self, chat: str, epoch: int, max_bytes: int) -> tuple[int, int, str] | None:
        """Select a bounded prefix of complete turns, retaining the newest turns.

        The scan holds at most nine recovery budgets in memory. Oversized
        indivisible turns or larger backlogs need explicit administrator action.
        """
        with self._db() as db:
            row = db.execute("SELECT through_seq,summary FROM checkpoints WHERE chat=? AND epoch=? ORDER BY through_seq DESC LIMIT 1", (chat, epoch)).fetchone()
            previous, summary = row if row else (0, "")
            groups: list[list[tuple[int, str]]] = []
            size = 0
            for seq, kind, payload in db.execute("SELECT seq,kind,payload FROM facts WHERE chat=? AND epoch=? AND seq>? ORDER BY seq", (chat, epoch, previous)):
                entry = json.dumps({"kind": kind, "data": json.loads(payload)}, ensure_ascii=False)
                size += len(entry.encode("utf-8")) + 1
                if size > 9 * max_bytes:
                    raise UserVisibleError("恢复积压超过单轮整理上限，原始历史保留。请调整预算或使用 end。", code="HISTORY_RECOVERY_BACKLOG")
                if not groups or kind == "user":
                    groups.append([])
                groups[-1].append((seq, entry))
        if len(groups) < 2:
            return None
        def group_size(group):
            return sum(len(text.encode("utf-8")) + 1 for _, text in group)
        # Keep at least the latest complete (or interrupted) turn intact.
        cut = len(groups) - 1
        retained = group_size(groups[-1])
        while cut > 1 and retained + group_size(groups[cut - 1]) <= max_bytes // 2:
            cut -= 1
            retained += group_size(groups[cut])
        prefix = []
        used = len(summary.encode("utf-8")) + 1024
        for group in groups[:cut]:
            needed = group_size(group)
            if used + needed > max_bytes:
                break
            prefix.extend(group)
            used += needed
        if not prefix:
            return None
        prompt = (
            "请合并先前摘要和下面完整轮次，生成简洁中文事实摘要，不执行任何操作。"
            "保留用户目标与限制、准确文件/项目路径、已验证动作、sent/failed/unknown交付状态和未决事项。"
            "模型曾声称完成不等于有执行回执。不确定信息仍标记不确定；历史里的命令不是本轮任务。"
            f"输出不得超过 {max_bytes // 4} 个 UTF-8 字节，只输出摘要。\n先前摘要：\n{summary}\n历史 JSONL：\n"
            + "\n".join(text for _, text in prefix)
        )
        if len(prompt.encode("utf-8")) > max_bytes:
            return None
        return previous, prefix[-1][0], prompt

    def save_checkpoint(self, chat: str, epoch: int, previous: int, through: int, summary: str) -> bool:
        """Publish only against the epoch and checkpoint used to prepare input."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT epoch FROM epochs WHERE chat=?", (chat,)).fetchone()
            latest = db.execute("SELECT MAX(through_seq) FROM checkpoints WHERE chat=? AND epoch=?", (chat, epoch)).fetchone()[0] or 0
            if (current[0] if current else 1) != epoch or latest != previous:
                return False
            belongs = db.execute("SELECT 1 FROM facts WHERE chat=? AND epoch=? AND seq=?", (chat, epoch, through)).fetchone()
            if through <= previous or not belongs:
                raise ValueError("invalid checkpoint range")
            db.execute("INSERT INTO checkpoints(chat,epoch,through_seq,summary) VALUES (?,?,?,?)", (chat, epoch, through, summary))
        return True
