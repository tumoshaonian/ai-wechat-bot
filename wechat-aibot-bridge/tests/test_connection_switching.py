"""Offline tests for isolated credential probes and rollback-safe switching."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from wechat_agent.admin.api import API_PREFIX, create_app
from wechat_agent.admin.config import AdminSettings
from wechat_agent.admin.connection_probe import (
    ConnectionProbeResult,
    WeComCredentialProbe,
)
from wechat_agent.admin.security import SecretBox
from wechat_agent.admin.store import AdminStore


def probe_result(ok: bool) -> ConnectionProbeResult:
    status = "SUCCEEDED" if ok else "FAILED"
    return ConnectionProbeResult(
        ok=ok,
        code="AUTHENTICATED" if ok else "AUTHENTICATION_FAILED",
        phase="authentication",
        message="Authentication succeeded." if ok else "Authentication failed.",
        stages={
            "configuration": {"status": "SUCCEEDED"},
            "sdk": {"status": "SUCCEEDED"},
            "network": {"status": "SUCCEEDED"},
            "authentication": {"status": status},
        },
        duration_ms=3,
    )


class FakeProbe:
    def __init__(self, result: ConnectionProbeResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str, float]] = []

    async def probe(self, bot_id: str, secret: str, *, timeout_seconds: float = 12.0):
        self.calls.append((bot_id, secret, timeout_seconds))
        return self.result


class FakeSdkClient:
    def __init__(self, bot_id: str, secret: str, outcome: str, **_kwargs) -> None:
        self.bot_id, self.secret, self.outcome = bot_id, secret, outcome
        self.handlers = {}
        self.disconnected = False

    def on(self, event, handler):
        self.handlers[event] = handler

    async def connect(self):
        self.handlers["connected"]()
        if self.outcome == "success":
            self.handlers["authenticated"]()
        elif self.outcome == "auth-failure":
            self.handlers["error"](
                Exception(f"Authentication failed secret={self.secret}")
            )
        return self

    async def disconnect(self):
        self.disconnected = True


class CredentialProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_uses_disposable_sdk_client_and_disconnects(self) -> None:
        clients = []

        def factory(bot_id, secret, **kwargs):
            client = FakeSdkClient(bot_id, secret, "success", **kwargs)
            clients.append(client)
            return client

        result = await WeComCredentialProbe(factory).probe("bot", "private")
        self.assertTrue(result.ok)
        self.assertEqual("SUCCEEDED", result.stages["authentication"]["status"])
        self.assertTrue(clients[0].disconnected)

    async def test_auth_failure_is_redacted_and_always_disconnects(self) -> None:
        clients = []

        def factory(bot_id, secret, **kwargs):
            client = FakeSdkClient(bot_id, secret, "auth-failure", **kwargs)
            clients.append(client)
            return client

        result = await WeComCredentialProbe(factory).probe("bot", "never-leak")
        self.assertFalse(result.ok)
        self.assertEqual("AUTHENTICATION_FAILED", result.code)
        self.assertNotIn("never-leak", str(result.public()))
        self.assertTrue(clients[0].disconnected)

    async def test_timeout_reports_authentication_stage_and_disconnects(self) -> None:
        clients = []

        def factory(bot_id, secret, **kwargs):
            client = FakeSdkClient(bot_id, secret, "hang", **kwargs)
            clients.append(client)
            return client

        result = await WeComCredentialProbe(factory).probe(
            "bot", "private", timeout_seconds=0.01
        )
        self.assertFalse(result.ok)
        self.assertEqual("AUTHENTICATION_TIMEOUT", result.code)
        self.assertTrue(clients[0].disconnected)


class ConnectionSwitchingApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = AdminSettings(
            database_path=root / "admin.db",
            master_key_path=root / "master.key",
            static_directory=root / "missing-static",
            runtime_control_enabled=False,
            connection_probe_timeout_seconds=1.0,
        )
        self.store = AdminStore(
            self.settings.database_path, SecretBox.load(self.settings.master_key_path)
        )

    def tearDown(self) -> None:
        if hasattr(self, "client"):
            self.client.close()
        self.temporary.cleanup()

    def _client(self, result: ConnectionProbeResult) -> tuple[TestClient, FakeProbe, dict]:
        fake = FakeProbe(result)
        self.client = TestClient(create_app(self.settings, self.store, connection_probe=fake))
        self.client.post(
            f"{API_PREFIX}/setup",
            json={"username": "admin", "password": "StrongPassword!123"},
        )
        login = self.client.post(
            f"{API_PREFIX}/auth/login",
            json={"username": "admin", "password": "StrongPassword!123", "mode": "token"},
        ).json()
        headers = {"Authorization": f"Bearer {login['access_token']}"}
        return self.client, fake, headers

    def _connections(self, client: TestClient, headers: dict) -> tuple[dict, dict]:
        first = client.post(
            f"{API_PREFIX}/connections",
            headers=headers,
            json={"name": "Current", "bot_id": "old-bot", "secret": "old-private"},
        ).json()
        candidate = client.post(
            f"{API_PREFIX}/connections",
            headers=headers,
            json={"name": "Candidate", "bot_id": "new-bot", "secret": "new-private"},
        ).json()
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE channel_connections SET is_active=1,status='ONLINE' WHERE id=?",
                (first["id"],),
            )
        return first, candidate

    def test_failed_probe_does_not_change_active_connection(self) -> None:
        client, fake, headers = self._client(probe_result(False))
        current, candidate = self._connections(client, headers)
        observation = client.post(
            f"{API_PREFIX}/connections/{current['id']}/test", headers=headers
        )
        self.assertEqual(200, observation.status_code, observation.text)
        self.assertEqual("LIVE_RUNTIME_OBSERVATION", observation.json()["mode"])
        self.assertEqual([], fake.calls)
        response = client.post(
            f"{API_PREFIX}/connections/{candidate['id']}/activate", headers=headers
        )
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("CONNECTION_CANDIDATE_REJECTED", response.json()["detail"]["code"])
        self.assertTrue(self.store.get_record("connections", current["id"])["is_active"])
        self.assertFalse(self.store.get_record("connections", candidate["id"])["is_active"])
        self.assertEqual("new-private", fake.calls[0][1])
        self.assertNotIn("new-private", response.text)
        with self.store.database.connect() as connection:
            self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM control_commands").fetchone()[0])

    def test_success_is_atomic_and_restart_failure_rolls_back(self) -> None:
        client, _fake, headers = self._client(probe_result(True))
        current, candidate = self._connections(client, headers)
        response = client.post(
            f"{API_PREFIX}/connections/{candidate['id']}/activate", headers=headers
        )
        self.assertEqual(202, response.status_code, response.text)
        body = response.json()
        self.assertEqual("PENDING_RESTART_AND_AUTHENTICATION", body["activation_state"])
        self.assertEqual("ACTIVATING", self.store.get_record("connections", candidate["id"])["status"])
        protected_previous = client.delete(
            f"{API_PREFIX}/connections/{current['id']}", headers=headers
        )
        self.assertEqual(409, protected_previous.status_code)
        active_candidate = self.store.get_record("connections", candidate["id"])
        credential_edit = client.patch(
            f"{API_PREFIX}/connections/{candidate['id']}",
            headers=headers,
            json={"version": active_candidate["version"], "secret": "unverified-change"},
        )
        self.assertEqual(409, credential_edit.status_code)
        command = self.store.get_control_command(body["control_command"]["id"])
        self.assertEqual("RESTART_SERVICE", command["command_type"])
        self.assertEqual(current["id"], command["payload"]["previous_connection_id"])
        self.assertNotIn("private", str(command))
        claimed = self.store.claim_control_commands("supervisor", command_types={"RESTART_SERVICE"})
        self.assertEqual(command["id"], claimed[0]["id"])
        self.assertTrue(
            self.store.complete_control_command(
                command["id"], success=False, error="restart failed", worker_id="supervisor"
            )
        )
        self.assertTrue(self.store.get_record("connections", current["id"])["is_active"])
        self.assertFalse(self.store.get_record("connections", candidate["id"])["is_active"])
        self.assertIsNone(self.store.get_pending_connection_activation())

    def test_runtime_auth_failure_rolls_back_but_success_confirms(self) -> None:
        client, _fake, headers = self._client(probe_result(True))
        current, candidate = self._connections(client, headers)
        switched = client.post(
            f"{API_PREFIX}/connections/{candidate['id']}/activate", headers=headers
        ).json()
        self.store.record_event(
            "connection.authentication_failed",
            payload={
                "connection_id": candidate["id"],
                "phase": "authentication",
                "code": "AUTHENTICATION_FAILED",
                "error": "invalid credential",
            },
        )
        self.assertTrue(self.store.get_record("connections", current["id"])["is_active"])
        self.assertIsNone(self.store.get_pending_connection_activation())

        # Start a fresh candidate activation and confirm it only via a real runtime event.
        second = client.post(
            f"{API_PREFIX}/connections",
            headers=headers,
            json={"name": "Second", "bot_id": "second-bot", "secret": "second-private"},
        ).json()
        response = client.post(
            f"{API_PREFIX}/connections/{second['id']}/activate", headers=headers
        )
        self.assertEqual(202, response.status_code, response.text)
        self.store.record_event(
            "connection.authenticated", payload={"connection_id": second["id"]}
        )
        self.assertEqual("ONLINE", self.store.get_record("connections", second["id"])["status"])
        self.assertIsNone(self.store.get_pending_connection_activation())
        self.assertNotEqual(switched["activation_id"], response.json()["activation_id"])


if __name__ == "__main__":
    unittest.main()
