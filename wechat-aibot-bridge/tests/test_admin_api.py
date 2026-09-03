"""HTTP security and resource contract tests for the FastAPI control plane."""

import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from wechat_agent.admin.api import API_PREFIX, create_app
from wechat_agent.admin.config import AdminSettings
from wechat_agent.admin.security import SecretBox, hash_password
from wechat_agent.admin.store import AdminStore, utcnow


class AdminApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = AdminSettings(
            database_path=root / "admin.db",
            master_key_path=root / "master.key",
            static_directory=root / "missing-static",
            session_hours=1,
        )
        self.store = AdminStore(
            self.settings.database_path, SecretBox.load(self.settings.master_key_path)
        )
        self.client = TestClient(create_app(self.settings, self.store))

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def bootstrap_and_login(self, mode: str = "cookie") -> dict:
        setup = self.client.post(
            f"{API_PREFIX}/setup",
            json={
                "username": "admin",
                "password": "StrongPassword!123",
                "display_name": "Administrator",
            },
        )
        self.assertEqual(201, setup.status_code, setup.text)
        login = self.client.post(
            f"{API_PREFIX}/auth/login",
            json={
                "username": "admin",
                "password": "StrongPassword!123",
                "mode": mode,
            },
        )
        self.assertEqual(200, login.status_code, login.text)
        return login.json()

    def test_first_setup_is_single_use_and_health_is_public(self) -> None:
        self.assertTrue(self.client.get(f"{API_PREFIX}/health").json()["setup_required"])
        self.bootstrap_and_login()
        repeated = self.client.post(
            f"{API_PREFIX}/setup",
            json={"username": "other", "password": "AnotherStrong!123"},
        )
        self.assertEqual(409, repeated.status_code)
        self.assertEqual("SETUP_ALREADY_COMPLETED", repeated.json()["detail"]["code"])

    def test_cookie_auth_requires_csrf_for_mutation(self) -> None:
        login = self.bootstrap_and_login()
        self.assertEqual(200, self.client.get(f"{API_PREFIX}/dashboard/summary").status_code)
        body = {"name": "Primary", "bot_id": "bot", "secret": "secret-value"}
        denied = self.client.post(f"{API_PREFIX}/connections", json=body)
        self.assertEqual(403, denied.status_code)
        created = self.client.post(
            f"{API_PREFIX}/connections",
            json=body,
            headers={"X-CSRF-Token": login["csrf_token"]},
        )
        self.assertEqual(201, created.status_code, created.text)
        self.assertNotIn("secret", created.json())
        self.assertTrue(created.json()["secret_configured"])

    def test_bearer_auth_does_not_need_csrf_and_never_returns_secret(self) -> None:
        login = self.bootstrap_and_login(mode="token")
        headers = {"Authorization": f"Bearer {login['access_token']}"}
        created = self.client.post(
            f"{API_PREFIX}/connections",
            headers=headers,
            json={"name": "Primary", "bot_id": "bot", "secret": "private"},
        )
        self.assertEqual(201, created.status_code, created.text)
        listing = self.client.get(f"{API_PREFIX}/connections", headers=headers)
        serialized = listing.text
        self.assertNotIn("private", serialized)
        self.assertNotIn("secret_ciphertext", serialized)

    def test_viewer_role_is_read_only(self) -> None:
        self.bootstrap_and_login()
        user_id = str(uuid.uuid4())
        now = utcnow()
        with self.store.transaction() as connection:
            connection.execute(
                "INSERT INTO admin_users(id,username,display_name,password_hash,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (user_id, "viewer", "Viewer", hash_password("ViewerPassword!123"), now, now),
            )
            connection.execute(
                "INSERT INTO admin_user_roles(user_id,role_id) VALUES(?,?)",
                (user_id, "role:viewer"),
            )
        login = self.client.post(
            f"{API_PREFIX}/auth/login",
            json={"username": "viewer", "password": "ViewerPassword!123", "mode": "token"},
        ).json()
        headers = {"Authorization": f"Bearer {login['access_token']}"}
        self.assertEqual(200, self.client.get(f"{API_PREFIX}/dashboard/summary", headers=headers).status_code)
        denied = self.client.post(
            f"{API_PREFIX}/connections",
            headers=headers,
            json={"name": "Nope"},
        )
        self.assertEqual(403, denied.status_code)

    def test_end_session_command_contains_executable_session_id(self) -> None:
        login = self.bootstrap_and_login()
        self.store.record_event(
            "message.received",
            trace_id="trace",
            payload={
                "connection_id": "default",
                "message_id": "m",
                "sender_id": "owner",
                "chat_id": "owner",
                "chat_type": "single",
                "content": "hello",
            },
        )
        conversation = self.store.list_page(
            "conversations", page=1, page_size=20
        )["items"][0]
        response = self.client.post(
            f"{API_PREFIX}/conversations/{conversation['id']}/end",
            json={"reason": "new topic", "fresh_session": True},
            headers={"X-CSRF-Token": login["csrf_token"]},
        )
        self.assertEqual(202, response.status_code, response.text)
        command = self.store.get_control_command(response.json()["id"])
        self.assertEqual("wecom:single:owner", command["payload"]["session_id"])

    def test_retry_is_explicitly_unsupported_instead_of_fake_success(self) -> None:
        login = self.bootstrap_and_login()
        self.store.record_event(
            "task.failed", trace_id="trace", payload={"task_id": "failed", "error": "x"}
        )
        response = self.client.post(
            f"{API_PREFIX}/tasks/failed/retry",
            json={},
            headers={"X-CSRF-Token": login["csrf_token"]},
        )
        self.assertEqual(501, response.status_code)
        self.assertEqual("TASK_RETRY_NOT_SUPPORTED", response.json()["detail"]["code"])

    def test_file_delivery_retry_is_authorized_durable_and_idempotent(self) -> None:
        login = self.bootstrap_and_login()
        artifact_file = Path(self.temporary.name) / "report.txt"
        artifact_file.write_text("report", encoding="utf-8")
        self.store.ensure_environment_connection("primary", "bot", "secret")
        common = {
            "connection_id": "primary",
            "message_id": "message-file",
            "sender_id": "owner",
            "chat_id": "owner",
            "chat_type": "single",
            "task_id": "task-file-api",
        }
        self.store.record_event(
            "message.received",
            trace_id="trace-file-api",
            payload=common | {"content": "send report"},
        )
        self.store.record_event(
            "task.started",
            trace_id="trace-file-api",
            payload=common | {"content": "send report"},
        )
        self.store.record_event(
            "artifact.created",
            trace_id="trace-file-api",
            payload=common
            | {
                "artifact_id": "artifact-file-api",
                "name": artifact_file.name,
                "path": str(artifact_file),
                "status": "AVAILABLE",
            },
        )
        self.store.record_event(
            "artifact.delivery.failed",
            trace_id="trace-file-api",
            payload=common
            | {
                "artifact_id": "artifact-file-api",
                "delivery_id": "delivery-file-api",
                "error": "temporary failure",
            },
        )
        headers = {
            "X-CSRF-Token": login["csrf_token"],
            "Idempotency-Key": "retry-file-api-once",
        }
        first = self.client.post(
            f"{API_PREFIX}/deliveries/delivery-file-api/retry",
            json={},
            headers=headers,
        )
        repeated = self.client.post(
            f"{API_PREFIX}/deliveries/delivery-file-api/retry",
            json={},
            headers=headers,
        )
        self.assertEqual(202, first.status_code, first.text)
        self.assertEqual(first.json()["id"], repeated.json()["id"])
        self.assertEqual("RESEND_FILE", first.json()["command_type"])
        self.assertEqual("PENDING", first.json()["status"])
        self.assertTrue(first.json()["accepted"])
        self.assertNotIn(str(artifact_file), first.text)

    def test_config_backup_admin_roles_and_runtime_endpoints(self) -> None:
        login = self.bootstrap_and_login()
        headers = {"X-CSRF-Token": login["csrf_token"]}
        profile = self.client.post(
            f"{API_PREFIX}/config-profiles",
            json={"name": "Default", "description": "main"},
            headers=headers,
        )
        self.assertEqual(201, profile.status_code, profile.text)
        revision = self.client.post(
            f"{API_PREFIX}/config-profiles/{profile.json()['id']}/revisions",
            json={
                "provider": "deepseek", "model": "v1",
                "system_prompt": "safe agent", "request_timeout_seconds": 900,
                "task_timeout_seconds": 480, "tool_policy": {"shell": False},
            },
            headers=headers,
        )
        self.assertEqual(201, revision.status_code, revision.text)
        published = self.client.post(
            f"{API_PREFIX}/config-profiles/{profile.json()['id']}/revisions/{revision.json()['id']}/publish",
            json={}, headers=headers,
        )
        self.assertTrue(published.json()["needs_restart"])
        backup = self.client.post(f"{API_PREFIX}/system/backup", json={}, headers=headers)
        self.assertEqual(201, backup.status_code, backup.text)
        self.assertEqual("ok", backup.json()["integrity"])
        roles = self.client.get(f"{API_PREFIX}/roles")
        self.assertGreaterEqual(roles.json()["total"], 5)
        created = self.client.post(
            f"{API_PREFIX}/admin-users",
            json={
                "username": "operator", "display_name": "Operator",
                "password": "OperatorPassword!123", "roles": ["operator"],
            },
            headers=headers,
        )
        self.assertEqual(["operator"], created.json()["roles"])
        command = self.client.post(
            f"{API_PREFIX}/runtime/services/bridge/restart",
            json={}, headers={**headers, "Idempotency-Key": "restart-bridge-1"},
        )
        self.assertEqual(202, command.status_code)
        self.assertEqual("PENDING", command.json()["status"])


if __name__ == "__main__":
    unittest.main()
