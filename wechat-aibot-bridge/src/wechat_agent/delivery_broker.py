"""Loopback-only file delivery bridge for an owned Harness MCP process."""

from __future__ import annotations

import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


class DeliveryBroker:
    def __init__(self, deliver: Callable[[str, str], dict], confirm=None, authorize=None) -> None:
        self.token = secrets.token_urlsafe(32)
        token = self.token

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass  # Never log credentials or paths from HTTP requests.

            def do_POST(self):
                self.connection.settimeout(10)
                if self.path not in {"/deliver", "/confirm", "/authorize"} or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                    self.send_error(403)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 16384:
                        raise ValueError("invalid request size")
                    body = json.loads(self.rfile.read(length))
                    if self.path == "/authorize":
                        if authorize is None:
                            raise ValueError("execution policy unavailable")
                        result = authorize(body)
                        self._respond(result)
                        return
                    field = "path" if self.path == "/deliver" else "operation"
                    if not isinstance(body, dict) or set(body) != {"task_ticket", field}:
                        raise ValueError("invalid request fields")
                    if not isinstance(body["task_ticket"], str) or not body["task_ticket"]:
                        raise ValueError("ticket must be a nonempty string")
                    if field == "path":
                        if not isinstance(body[field], str) or not body[field]:
                            raise ValueError("invalid path")
                        result = deliver(body["task_ticket"], body[field])
                    elif confirm is not None:
                        result = confirm(body["task_ticket"], body[field])
                    else:
                        raise ValueError("confirmation is unavailable")
                except Exception:
                    result = {"ok": False, "approved": False, "status": "rejected", "error": "请求被拒绝；检查当前任务票据、授权和参数。"}
                self._respond(result)

            def _respond(self, result):
                encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except (ConnectionError, TimeoutError):
                    # Reject/stop closes the owned Runtime before returning a decision.
                    # Its disconnected socket is expected; never retry a side effect.
                    pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/deliver"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="delivery-broker")
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
