"""Integration tests for the SQLite-to-Local-Supervisor command bridge."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from wechat_agent.admin.api import API_PREFIX, create_app
from wechat_agent.admin.config import AdminSettings
from wechat_agent.admin.runtime_worker import (
    SupervisorFileClient,
    SupervisorRuntimeCommandWorker,
)
from wechat_agent.admin.security import SecretBox
from wechat_agent.admin.store import AdminStore


class AdminRuntimeWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "supervisor"
        self.store = AdminStore(
            self.root / "admin.db", SecretBox.load(self.root / "master.key")
        )
        self.client = SupervisorFileClient(
            self.runtime,
            command_timeout_seconds=2,
            status_stale_seconds=10,
            response_poll_seconds=0.02,
        )
        self._write_status()

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    def _write_status(
        self,
        *,
        bridge_status: str = "running",
        bridge_health: str = "healthy",
    ) -> None:
        self.runtime.mkdir(parents=True, exist_ok=True)
        value = {
            "schemaVersion": 1,
            "generatedAtUtc": datetime.now(timezone.utc).isoformat(),
            "supervisor": {
                "status": "running",
                "instanceId": "supervisor-test",
                "pid": 1234,
            },
            "services": {
                "admin": {
                    "status": "running",
                    "managed": True,
                    "enabled": True,
                    "pid": 111,
                    "health": {"status": "healthy"},
                },
                "bridge": {
                    "status": bridge_status,
                    "managed": bridge_status == "running",
                    "enabled": True,
                    "pid": 222 if bridge_status == "running" else None,
                    "startedAtUtc": datetime.now(timezone.utc).isoformat(),
                    "health": {"status": bridge_health},
                },
            },
        }
        (self.runtime / "status.json").write_text(
            json.dumps(value), encoding="utf-8"
        )

    async def _fake_supervisor_once(
        self,
        command_id: str,
        *,
        success: bool = True,
        delay: float = 0,
    ) -> dict:
        request_path = self.client.request_path(command_id)
        deadline = asyncio.get_running_loop().time() + 2
        while not request_path.exists():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("runtime worker did not write a Supervisor request")
            await asyncio.sleep(0.01)
        request = json.loads(request_path.read_text(encoding="utf-8"))
        self.assertEqual(
            {"id", "action", "service", "requestedAtUtc", "clientPid"},
            set(request),
        )
        processing = self.client.processing_path(command_id)
        request_path.replace(processing)
        if delay:
            await asyncio.sleep(delay)
        action = request["action"]
        if success:
            if action == "restart":
                results = [
                    {"service": "bridge", "success": True, "status": "stopped"},
                    {"service": "bridge", "success": True, "status": "started", "pid": 333},
                ]
            elif action == "start":
                results = [{"service": "bridge", "success": True, "status": "started"}]
            else:
                results = [{"service": "bridge", "success": True, "status": "stopped"}]
            response = {**request, "success": True, "results": results}
        else:
            response = {
                **request,
                "success": False,
                "error": "ownership validation failed",
                "results": [],
            }
        self.client.responses_dir.mkdir(parents=True, exist_ok=True)
        self.client.response_path(command_id).write_text(
            json.dumps(response), encoding="utf-8"
        )
        processing.unlink(missing_ok=True)
        return request

    async def _wait_terminal(self, command_id: str, timeout: float = 3) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            command = self.store.get_control_command(command_id)
            if command["status"] in {"SUCCEEDED", "FAILED"}:
                return command
            if asyncio.get_running_loop().time() >= deadline:
                self.fail(f"command remained {command['status']}")
            await asyncio.sleep(0.02)

    async def test_restart_command_round_trip_and_live_status_projection(self) -> None:
        command = self.store.enqueue_control_command(
            "RESTART_SERVICE",
            "service",
            "bridge",
            payload={},
            idempotency_key="restart-integration",
            actor_id="admin",
            ip="127.0.0.1",
        )
        worker = SupervisorRuntimeCommandWorker(
            self.store,
            self.client,
            poll_seconds=0.02,
            lease_seconds=30,
            worker_id="runtime-integration",
        )
        await worker.start()
        fake = asyncio.create_task(self._fake_supervisor_once(command["id"]))
        try:
            terminal = await self._wait_terminal(command["id"])
            request = await fake
            deadline = asyncio.get_running_loop().time() + 1
            while self.client.response_path(command["id"]).exists():
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail("consumed Supervisor response was not removed")
                await asyncio.sleep(0.01)
        finally:
            await worker.close()
        self.assertEqual("restart", request["action"])
        self.assertEqual("SUCCEEDED", terminal["status"])
        self.assertEqual(2, len(terminal["result"]["results"]))
        self.assertFalse(self.client.response_path(command["id"]).exists())
        self.assertEqual("ONLINE", self.store.get_record("nodes", "local")["status"])
        services = self.store.list_page("services", page=1, page_size=20)["items"]
        bridge = next(item for item in services if item["service_type"] == "bridge")
        self.assertEqual("HEALTHY", bridge["status"])
        self.assertEqual(222, bridge["pid"])

    async def test_explicit_supervisor_failure_is_not_reported_as_success(self) -> None:
        command = self.store.enqueue_control_command(
            "START_SERVICE", "service", "bridge", payload={},
            idempotency_key="start-failure", actor_id="admin", ip=None,
        )
        worker = SupervisorRuntimeCommandWorker(
            self.store, self.client, poll_seconds=0.02, worker_id="failure-worker"
        )
        await worker.start()
        fake = asyncio.create_task(
            self._fake_supervisor_once(command["id"], success=False)
        )
        try:
            terminal = await self._wait_terminal(command["id"])
            await fake
        finally:
            await worker.close()
        self.assertEqual("FAILED", terminal["status"])
        self.assertIn("ownership validation failed", terminal["error_message"])

    async def test_unsafe_service_is_failed_without_writing_a_request(self) -> None:
        command = self.store.enqueue_control_command(
            "STOP_SERVICE", "service", "admin", payload={},
            idempotency_key="unsafe-admin-stop", actor_id="admin", ip=None,
        )
        worker = SupervisorRuntimeCommandWorker(
            self.store, self.client, poll_seconds=0.02, worker_id="safe-worker"
        )
        await worker.start()
        try:
            terminal = await self._wait_terminal(command["id"])
        finally:
            await worker.close()
        self.assertEqual("FAILED", terminal["status"])
        self.assertIn("unsafe runtime target", terminal["error_message"])
        self.assertFalse(self.client.request_path(command["id"]).exists())

    async def test_missing_response_times_out_instead_of_claiming_success(self) -> None:
        self.client.command_timeout_seconds = 0.15
        command = self.store.enqueue_control_command(
            "START_SERVICE", "service", "bridge", payload={},
            idempotency_key="response-timeout", actor_id="admin", ip=None,
        )
        worker = SupervisorRuntimeCommandWorker(
            self.store, self.client, poll_seconds=0.02, worker_id="timeout-worker"
        )
        await worker.start()
        try:
            terminal = await self._wait_terminal(command["id"])
        finally:
            await worker.close()
        self.assertEqual("FAILED", terminal["status"])
        self.assertIn("TimeoutError", terminal["error_message"])
        self.assertTrue(self.client.request_path(command["id"]).exists())

    async def test_success_ack_database_failure_preserves_recoverable_response(self) -> None:
        command = self.store.enqueue_control_command(
            "START_SERVICE", "service", "bridge", payload={},
            idempotency_key="success-ack-db-failure", actor_id="admin", ip=None,
        )
        worker_id = "persistence-failure-worker"
        claimed = self.store.claim_control_commands(
            worker_id, command_types={"START_SERVICE"}, limit=1
        )[0]
        response = {
            "id": command["id"],
            "action": "start",
            "service": "bridge",
            "success": True,
            "results": [
                {"service": "bridge", "success": True, "status": "started"}
            ],
        }
        self.client.response_path(command["id"]).write_text(
            json.dumps(response), encoding="utf-8"
        )
        original = self.store.complete_control_command

        def fail_success_ack(command_id: str, **kwargs):
            if kwargs.get("success"):
                raise sqlite3.OperationalError("database temporarily unavailable")
            return original(command_id, **kwargs)

        self.store.complete_control_command = fail_success_ack  # type: ignore[method-assign]
        worker = SupervisorRuntimeCommandWorker(
            self.store, self.client, worker_id=worker_id
        )
        try:
            await worker._execute(claimed)
            persisted = self.store.get_control_command(command["id"])
            self.assertEqual("RUNNING", persisted["status"])
            self.assertTrue(self.client.response_path(command["id"]).exists())
        finally:
            self.store.complete_control_command = original  # type: ignore[method-assign]
            await worker.close()

    async def test_graceful_api_restart_releases_and_resumes_exact_request(self) -> None:
        command = self.store.enqueue_control_command(
            "STOP_SERVICE", "service", "bridge", payload={},
            idempotency_key="restart-recovery", actor_id="admin", ip=None,
        )
        first = SupervisorRuntimeCommandWorker(
            self.store, self.client, poll_seconds=0.02, worker_id="api-before-restart"
        )
        await first.start()
        deadline = asyncio.get_running_loop().time() + 2
        while not self.client.request_path(command["id"]).exists():
            if asyncio.get_running_loop().time() > deadline:
                self.fail("first API worker did not dispatch")
            await asyncio.sleep(0.01)
        self.assertEqual(
            "RUNNING", self.store.get_control_command(command["id"])["status"]
        )
        await first.close()
        self.assertEqual(
            "PENDING", self.store.get_control_command(command["id"])["status"]
        )

        second = SupervisorRuntimeCommandWorker(
            self.store, self.client, poll_seconds=0.02, worker_id="api-after-restart"
        )
        await second.start()
        fake = asyncio.create_task(self._fake_supervisor_once(command["id"]))
        try:
            terminal = await self._wait_terminal(command["id"])
            await fake
        finally:
            await second.close()
        self.assertEqual("SUCCEEDED", terminal["status"])

    async def test_stale_status_marks_node_and_services_unavailable(self) -> None:
        status = json.loads((self.runtime / "status.json").read_text(encoding="utf-8"))
        status["generatedAtUtc"] = "2000-01-01T00:00:00+00:00"
        (self.runtime / "status.json").write_text(json.dumps(status), encoding="utf-8")
        worker = SupervisorRuntimeCommandWorker(
            self.store, self.client, poll_seconds=0.02, worker_id="stale-status"
        )
        await worker.start()
        try:
            deadline = asyncio.get_running_loop().time() + 2
            while (
                self.store.list_page("nodes", page=1, page_size=10)["total"] == 0
                or self.store.list_page("services", page=1, page_size=10)["total"] < 2
            ):
                if asyncio.get_running_loop().time() > deadline:
                    self.fail("stale status was not projected")
                await asyncio.sleep(0.01)
        finally:
            await worker.close()
        self.assertEqual("OFFLINE", self.store.get_record("nodes", "local")["status"])
        service_states = {
            item["service_type"]: item["status"]
            for item in self.store.list_page("services", page=1, page_size=10)["items"]
        }
        self.assertEqual("SUPERVISOR_UNAVAILABLE", service_states["bridge"])


class AdminRuntimeLifespanTests(unittest.TestCase):
    def test_fastapi_lifespan_starts_and_stops_injected_worker(self) -> None:
        class Probe:
            def __init__(self) -> None:
                self.started = False
                self.closed = False

            async def start(self) -> None:
                self.started = True

            async def close(self) -> None:
                self.closed = True

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = AdminSettings(
                database_path=root / "admin.db",
                master_key_path=root / "master.key",
                static_directory=root / "missing",
            )
            store = AdminStore(
                settings.database_path, SecretBox.load(settings.master_key_path)
            )
            probe = Probe()
            with TestClient(create_app(settings, store, runtime_worker=probe)) as client:  # type: ignore[arg-type]
                self.assertTrue(probe.started)
                self.assertEqual(200, client.get(f"{API_PREFIX}/health/live").status_code)
            self.assertTrue(probe.closed)

    def test_api_projects_unavailable_supervisor_and_rejects_unsafe_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = AdminSettings(
                database_path=root / "admin.db",
                master_key_path=root / "master.key",
                static_directory=root / "missing",
                supervisor_runtime_dir=root / "supervisor",
                supervisor_poll_seconds=0.05,
            )
            store = AdminStore(
                settings.database_path, SecretBox.load(settings.master_key_path)
            )
            with TestClient(create_app(settings, store)) as client:
                setup = client.post(
                    f"{API_PREFIX}/setup",
                    json={
                        "username": "admin",
                        "password": "StrongPassword!123",
                        "display_name": "Administrator",
                    },
                )
                self.assertEqual(201, setup.status_code, setup.text)
                login = client.post(
                    f"{API_PREFIX}/auth/login",
                    json={
                        "username": "admin",
                        "password": "StrongPassword!123",
                        "mode": "cookie",
                    },
                )
                self.assertEqual(200, login.status_code, login.text)
                csrf = login.json()["csrf_token"]
                nodes = client.get(f"{API_PREFIX}/nodes").json()["items"]
                services = client.get(f"{API_PREFIX}/services").json()["items"]
                self.assertEqual("OFFLINE", nodes[0]["status"])
                self.assertEqual(
                    {"SUPERVISOR_UNAVAILABLE"},
                    {item["status"] for item in services},
                )
                rejected = client.post(
                    f"{API_PREFIX}/runtime/services/desktop/restart",
                    json={},
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(404, rejected.status_code, rejected.text)
                accepted = client.post(
                    f"{API_PREFIX}/runtime/services/bridge/restart",
                    json={},
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(202, accepted.status_code, accepted.text)


if __name__ == "__main__":
    unittest.main()
