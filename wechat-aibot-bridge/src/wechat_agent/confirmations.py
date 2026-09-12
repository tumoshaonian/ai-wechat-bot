"""Bounded, one-shot human decisions, scoped to live task and logical epoch.

This is a confirmation handshake, not a replacement for tool execution policy.
Persisted decisions are audit evidence; they are never replayed after restart.
"""
import asyncio
import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager


class ConfirmationCoordinator:
    def __init__(self, journal, *, timeout_seconds=90, clock=time.time):
        self.journal = journal
        self.timeout = timeout_seconds
        self.clock = clock
        self.owner = secrets.token_hex(16)
        self.waiters = {}
        with self.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS confirmations(
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, chat TEXT NOT NULL,
                sender TEXT NOT NULL, task TEXT NOT NULL, epoch INTEGER NOT NULL,
                operation TEXT NOT NULL, digest TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, expires REAL NOT NULL, decided REAL)""")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.journal.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            with db:
                yield db
        finally:
            db.close()

    def waiting(self, *, task_id=None, session_id=None):
        return any((task_id is None or message.task_id == task_id) and
                   (session_id is None or message.session_id == session_id)
                   for message, _ in list(self.waiters.values()))

    async def request(self, message, epoch, operation, notify, event):
        if not isinstance(operation, dict) or set(operation) != {"action", "target", "effect"}:
            raise ValueError("operation requires action, target and effect")
        for key, limit in (("action", 200), ("target", 1000), ("effect", 2000)):
            value = operation[key]
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError("invalid confirmation description")
        if self.waiting(task_id=message.task_id) or self.journal.epoch(message.session_id) != epoch:
            raise ValueError("task already waiting or epoch changed")
        canonical = json.dumps(operation, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        code = secrets.token_hex(8)
        now = self.clock()
        with self.db() as db:
            db.execute("INSERT INTO confirmations VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)", (
                code, self.owner, message.session_id, message.sender_id, message.task_id,
                epoch, canonical, digest, "pending", now, now + self.timeout,
            ))
        future = asyncio.get_running_loop().create_future()
        self.waiters[code] = (message, future)
        event("task.waiting", {"state": "waiting_confirmation", "confirmation_id": code, "operation_digest": digest,
              "operation": json.loads(canonical), "requested_user": message.sender_id, "expires_at": now + self.timeout})
        status = "cancelled"
        try:
            # A proposal is untrusted agent text, rendered as plain channel text.
            await asyncio.wait_for(notify(
                f"需要你确认本次操作（{int(self.timeout)} 秒内有效）：\n"
                f"操作：{operation['action']}\n目标：{operation['target']}\n影响：{operation['effect']}\n"
                f"仅同意上述操作请回复 /确认 {code}\n不同意请回复 /拒绝 {code}\n"
                "确认只限本次描述，不授权其他操作。"
            ), min(10, self.timeout))
            try:
                status = await asyncio.wait_for(asyncio.shield(future), max(0.001, now + self.timeout - self.clock()))
            except asyncio.TimeoutError:
                status = "expired"
            return {"ok": status == "approved", "approved": status == "approved",
                    "status": status, "confirmation_id": code, "operation_digest": digest,
                    "operation": json.loads(canonical),
                    "scope": "仅适用于这个任务中展示的操作；其他操作必须重新确认。未批准时不得执行。"}
        finally:
            self.waiters.pop(code, None)
            if not future.done():
                future.cancel()
            with self.db() as db:
                db.execute("UPDATE confirmations SET status=?,decided=? WHERE id=? AND status='pending'", (status, self.clock(), code))
            self.journal.append(message.session_id, epoch, "confirmation", {
                "confirmation_id": code, "operation": json.loads(canonical), "status": status,
                "operation_digest": digest, "meaning": "仅记录本次确认交互，不代表操作已执行或完成，不可复用为新的授权。",
            })
            event("confirmation.resolved", {"confirmation_id": code, "status": status, "operation_digest": digest,
                  "decided_by": message.sender_id if status in {"approved", "rejected"} else None})
            if status == "approved":
                event("task.resumed", {"state": "running", "confirmation_id": code, "decision": status})

    def decide(self, message, code, approve):
        live = self.waiters.get(code)
        if live is None:
            return False, "该编号不在当前进程的等待列表中，可能已处理、过期或服务已重启。"
        with self.db() as db:
            row = db.execute("SELECT * FROM confirmations WHERE id=?", (code,)).fetchone()
            epoch = db.execute("SELECT epoch FROM epochs WHERE chat=?", (message.session_id,)).fetchone()
            current = epoch[0] if epoch else 1
            if (row is None or row["owner"] != self.owner or row["chat"] != message.session_id
                    or row["sender"] != message.sender_id or row["epoch"] != current or row["status"] != "pending"):
                return False, "确认与当前用户或会话不匹配，或已经处理。"
            status = "expired" if self.clock() >= row["expires"] else ("approved" if approve else "rejected")
            db.execute("UPDATE confirmations SET status=?,decided=? WHERE id=? AND status='pending'", (status, self.clock(), code))
        if not live[1].done():
            live[1].set_result(status)
        return status != "expired", {"approved": "已确认本次操作。", "rejected": "已拒绝，本次操作不得执行。", "expired": "确认已过期，本次操作不得执行。"}[status]

    def cancel_task(self, task_id):
        for code, (message, future) in list(self.waiters.items()):
            if message.task_id != task_id:
                continue
            with self.db() as db:
                db.execute("UPDATE confirmations SET status='cancelled',decided=? WHERE id=? AND status='pending'", (self.clock(), code))
            if not future.done():
                future.set_result("cancelled")
