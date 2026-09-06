"""One task's authorized, deduplicated delivery attempts and receipts."""

import asyncio
import hashlib
from pathlib import Path


class FileDeliverySession:
    def __init__(self, message, responder, store=None):
        self.message = message
        self.responder = responder
        self.results: dict[str, dict] = {}
        self._lock = asyncio.Lock()
        self.store = store

    async def send(self, path: Path) -> dict:
        policy = self.message.access_policy
        if policy.get("can_send_files") is False or policy.get("can_read_files") is False:
            return {"ok": False, "status": "rejected", "error": "当前账号没有读取并发送本地文件的权限。"}
        if not path.is_absolute():
            return {"ok": False, "status": "rejected", "error": "需要绝对文件路径。"}
        try:
            path = path.resolve(strict=True)
            if not path.is_file() or not 0 < path.stat().st_size <= 50 * 1024 * 1024:
                raise ValueError("需要非空、大小不超过50MiB的普通文件")
            def digest():
                with path.open("rb") as stream:
                    return hashlib.file_digest(stream, "sha256").hexdigest()
            sha = await asyncio.to_thread(digest)
        except (OSError, ValueError) as exc:
            return {"ok": False, "status": "rejected", "error": str(exc)}
        key = f"{path}:{sha}"
        async with self._lock:
            if key in self.results:
                return self.results[key] | {"duplicate": True}
            result = {"ok": False, "status": "unknown", "name": path.name, "path": str(path), "sha256": sha}
            task_key = f"{self.message.session_id}:{self.message.task_id or self.message.message_id}"
            if self.store is not None:
                previous = self.store.claim_delivery(task_key, key, result)
                if previous is not None:
                    self.results[key] = previous
                    return previous | {"duplicate": True}
            self.results[key] = result
            try:
                await self.responder.send_file(path)
            except asyncio.CancelledError:
                result["error"] = "发送被中断，回执未知；请核实后再发起新的交付。"
                raise
            except Exception:
                result["error"] = "发送失败或回执未知，禁止盲目重发。"
            else:
                result.update(ok=True, status="sent")
            if self.store is not None:
                self.store.finish_delivery(task_key, key, result)
            return dict(result)

    def names(self, *, sent: bool) -> list[str]:
        return [r["name"] for r in self.results.values() if (r["status"] == "sent") == sent]
