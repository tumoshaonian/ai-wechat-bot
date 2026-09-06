"""Loopback-only file delivery bridge for an owned Harness MCP process."""

from __future__ import annotations

import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


class DeliveryBroker:
    def __init__(self, deliver: Callable[[str, str], dict]) -> None:
        self.token = secrets.token_urlsafe(32)
        token = self.token

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # Never log credentials or paths from HTTP requests.

            def do_POST(self):
                self.connection.settimeout(10)
                if self.path != "/deliver" or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 16384:
                        raise ValueError("invalid request size")
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict) or set(body) != {"task_ticket", "path"}:
                        raise ValueError("expected task_ticket and path")
                    if not all(isinstance(body[k], str) and body[k] for k in body):
                        raise ValueError("ticket and path must be nonempty strings")
                    result = deliver(body["task_ticket"], body["path"])
                except Exception:
                    result = {"ok": False, "status": "rejected", "error": "交付请求被拒绝；检查当前任务票据、授权与文件路径。"}
                encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/deliver"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="delivery-broker")
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
