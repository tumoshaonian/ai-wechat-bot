"""Persistence, projection, encryption and control-queue tests."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from wechat_agent.admin.redaction import redact_data
from wechat_agent.admin.security import SecretBox, hash_password, verify_password
from wechat_agent.admin.store import AdminStore


class AdminStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = AdminStore(
            self.root / "admin.db", SecretBox.load(self.root / "master.key")
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_password_hash_and_secret_envelope_are_not_plaintext(self) -> None:
        encoded = hash_password("StrongPassword!123")
        self.assertNotIn("StrongPassword!123", encoded)
        self.assertTrue(verify_password("StrongPassword!123", encoded))
        self.assertFalse(verify_password("wrong", encoded))

        connection = self.store.create_connection(
            {"name": "Primary", "bot_id": "bot-1", "secret": "top-secret"},
            actor_id="admin",
            ip="127.0.0.1",
        )
        self.assertTrue(connection["secret_configured"])
        self.assertNotIn("secret_ciphertext", connection)
        with self.store.database.connect() as database:
            envelope = database.execute(
                "SELECT secret_ciphertext FROM channel_connections WHERE id=?",
                (connection["id"],),
            ).fetchone()[0]
        self.assertNotIn("top-secret", envelope)
        self.assertEqual(
            "top-secret",
            self.store.secret_box.decrypt(envelope, context=connection["id"]),
        )

    def test_message_claim_is_durable_and_atomic(self) -> None:
        self.assertTrue(self.store.claim_message("connection", "message"))
        self.assertFalse(self.store.claim_message("connection", "message"))
        second_store = AdminStore(
            self.root / "admin.db", SecretBox.load(self.root / "master.key")
        )
        self.assertFalse(second_store.claim_message("connection", "message"))

    def test_event_projection_builds_conversation_task_and_tool_timeline(self) -> None:
        event = self.store.record_event(
            "message.received",
            trace_id="trace-1",
            payload={
                "connection_id": "primary",
                "message_id": "m-1",
                "sender_id": "owner",
                "chat_id": "owner",
                "chat_type": "single",
                "content": "hello",
            },
            idempotency_key="received:m-1",
        )
        duplicate = self.store.record_event(
            "message.received",
            trace_id="trace-1",
            payload={"connection_id": "primary", "message_id": "m-1"},
            idempotency_key="received:m-1",
        )
        self.assertEqual(event, duplicate)
        self.store.record_event(
            "task.started",
            trace_id="trace-1",
            payload={"task_id": "task-1", "content": "do work"},
        )
        self.store.record_event(
            "tool.started",
            trace_id="trace-1",
            payload={
                "task_id": "task-1",
                "tool_call_id": "tool-1",
                "tool_name": "shell",
                "input": {"api_key": "must-not-leak", "command": "pwd"},
            },
        )
        self.store.record_event(
            "tool.completed",
            trace_id="trace-1",
            payload={
                "task_id": "task-1",
                "tool_call_id": "tool-1",
                "tool_name": "shell",
                "output": {"exit_code": 0},
            },
        )
        self.store.record_event(
            "task.completed",
            trace_id="trace-1",
            payload={"task_id": "task-1", "result": "done"},
        )

        detail = self.store.task_detail("task-1")
        self.assertEqual("SUCCEEDED", detail["status"])
        self.assertEqual("SUCCEEDED", detail["tool_calls"][0]["status"])
        self.assertEqual("***", detail["tool_calls"][0]["input"]["api_key"])
        self.assertEqual(5, len(detail["events"]))
        self.assertEqual(1, self.store.list_page("conversations", page=1, page_size=20)["total"])

    def test_outbound_updates_do_not_collapse_and_progress_updates_task(self) -> None:
        self.store.record_event(
            "task.started", trace_id="trace", payload={"task_id": "task"}
        )
        self.store.record_event(
            "task.progress",
            trace_id="trace",
            payload={"task_id": "task", "content": "working"},
        )
        for content in ("working", "finished"):
            self.store.record_event(
                "message.outbound",
                trace_id="trace",
                payload={
                    "connection_id": "default",
                    "message_id": "same-inbound-id",
                    "chat_id": "owner",
                    "content": content,
                    "task_id": "task",
                },
            )
        self.assertEqual("RUNNING", self.store.task_detail("task")["status"])
        conversation = self.store.list_page(
            "conversations", page=1, page_size=20
        )["items"][0]
        messages = self.store.list_conversation_messages(conversation["id"], 1, 20)
        self.assertEqual(2, messages["total"])

    def test_task_links_to_conversation_and_artifact_path_is_encrypted_for_retry(self) -> None:
        artifact_file = self.root / "report.txt"
        artifact_file.write_text("report", encoding="utf-8")
        self.store.ensure_environment_connection(
            "primary", "bot-primary", "secret-primary"
        )
        common = {
            "connection_id": "primary",
            "message_id": "message-1",
            "sender_id": "owner",
            "chat_id": "owner",
            "chat_type": "single",
            "task_id": "task-file",
        }
        self.store.record_event(
            "message.received",
            trace_id="trace-file",
            payload=common | {"content": "send report"},
        )
        self.store.record_event(
            "task.started",
            trace_id="trace-file",
            payload=common | {"content": "send report"},
        )
        self.store.record_event(
            "artifact.created",
            trace_id="trace-file",
            payload=common
            | {
                "artifact_id": "artifact-1",
                "name": artifact_file.name,
                "path": str(artifact_file),
                "status": "AVAILABLE",
            },
        )
        self.store.record_event(
            "artifact.delivery.failed",
            trace_id="trace-file",
            payload=common
            | {
                "artifact_id": "artifact-1",
                "delivery_id": "delivery-1",
                "error": "temporary failure",
            },
        )

        task = self.store.task_detail("task-file")
        self.assertIsNotNone(task["conversation_id"])
        self.assertIsNotNone(task["message_id"])
        artifact = self.store.list_page(
            "artifacts", page=1, page_size=20
        )["items"][0]
        self.assertNotIn("path_ciphertext", artifact)
        self.assertNotIn(str(artifact_file), str(artifact))
        with self.store.database.connect() as database:
            encrypted = database.execute(
                "SELECT path_ciphertext FROM file_artifacts WHERE id='artifact-1'"
            ).fetchone()[0]
        self.assertNotIn(str(artifact_file), encrypted)
        artifact_event = next(
            event
            for event in self.store.fetch_events(0, 100)
            if event["event_type"] == "artifact.created"
        )
        self.assertNotIn(str(artifact_file), str(artifact_event["payload"]))

        command = self.store.enqueue_delivery_retry(
            "delivery-1", "retry-delivery-1", "admin", "127.0.0.1"
        )
        context = self.store.get_delivery_retry_context(command["target_id"])
        self.assertEqual(str(artifact_file), context["path"])
        self.assertEqual("owner", context["external_chat_id"])
        claimed = self.store.claim_control_commands(
            "bridge", command_types={"RESEND_FILE"}
        )
        self.assertEqual(command["id"], claimed[0]["id"])

    def test_partial_completion_and_connection_runtime_projection(self) -> None:
        self.store.record_event(
            "connection.online",
            payload={"connection_id": "runtime", "bot_id": "runtime-bot"},
        )
        connection = self.store.get_record("connections", "runtime")
        self.assertEqual("ONLINE", connection["status"])
        self.store.record_event(
            "task.completed",
            trace_id="partial-trace",
            payload={"task_id": "partial", "state": "partial_succeeded"},
        )
        self.assertEqual(
            "PARTIAL_SUCCEEDED", self.store.task_detail("partial")["status"]
        )

    def test_config_revisions_publish_and_rollback_without_overwriting_history(self) -> None:
        profile = self.store.create_config_profile(
            "Default", "main", "admin", "127.0.0.1"
        )
        first = self.store.create_config_revision(
            profile["id"],
            {
                "provider": "deepseek",
                "model": "v1",
                "system_prompt": "safe agent",
                "request_timeout_seconds": 900,
                "task_timeout_seconds": 480,
                "tool_policy": {"shell": False},
            },
            "admin",
            "127.0.0.1",
        )
        published = self.store.publish_config_revision(
            profile["id"], first["id"], "admin", "127.0.0.1"
        )
        self.assertEqual("PUBLISHED", published["status"])
        self.assertEqual(
            first["id"], self.store.get_active_runtime_config()["id"]
        )
        second = self.store.create_config_revision(
            profile["id"],
            {
                "provider": "deepseek",
                "model": "v2",
                "system_prompt": "new agent",
                "request_timeout_seconds": 800,
                "task_timeout_seconds": 400,
                "tool_policy": {"shell": True},
            },
            "admin",
            "127.0.0.1",
        )
        self.store.publish_config_revision(
            profile["id"], second["id"], "admin", "127.0.0.1"
        )
        rollback = self.store.rollback_config(
            profile["id"], first["id"], "admin", "127.0.0.1"
        )
        self.assertEqual("v1", rollback["model"])
        self.assertEqual(3, rollback["version"])
        self.assertEqual(3, len(self.store.get_config_profile(profile["id"])["revisions"]))
        self.assertEqual(
            rollback["id"], self.store.get_active_runtime_config()["id"]
        )

    def test_alert_lifecycle_and_runtime_projection(self) -> None:
        self.store.record_event(
            "task.failed",
            trace_id="failed-trace",
            payload={"task_id": "failed", "error": "offline"},
            severity="ERROR",
        )
        alert = self.store.list_page("alerts", page=1, page_size=20)["items"][0]
        acknowledged = self.store.update_alert(
            alert["id"], "acknowledge", "checking", "admin", "127.0.0.1"
        )
        self.assertEqual("ACKNOWLEDGED", acknowledged["status"])
        resolved = self.store.update_alert(
            alert["id"], "resolve", "fixed", "admin", "127.0.0.1"
        )
        self.assertEqual("RESOLVED", resolved["status"])
        self.store.record_event(
            "node.heartbeat",
            payload={"node_id": "local", "name": "PC", "capabilities": {"uia": True}},
        )
        self.store.record_event(
            "service.health",
            payload={"node_id": "local", "service": "bridge", "status": "HEALTHY", "pid": 123},
        )
        self.assertEqual("ONLINE", self.store.get_record("nodes", "local")["status"])
        self.assertEqual(1, self.store.list_page("services", page=1, page_size=20)["total"])

    def test_online_backup_retention_preview_and_environment_migration(self) -> None:
        credentials = self.store.ensure_environment_connection(
            "environment", "bot-id", "bot-secret"
        )
        self.assertEqual("bot-secret", credentials["secret"])
        # A later environment import must not overwrite the selected DB connection.
        credentials = self.store.ensure_environment_connection(
            "other", "other-bot", "other-secret"
        )
        self.assertEqual("bot-id", credentials["bot_id"])
        self.store.record_event("log.python", payload={"message": "hello"})
        preview = self.store.retention(
            event_days=90, log_days=30, session_days=30, audit_days=365,
            dry_run=True, actor_id="admin", ip="127.0.0.1",
        )
        self.assertTrue(preview["dry_run"])
        backup = self.store.backup_database(
            self.root / "backups", "admin", "127.0.0.1"
        )
        backup_path = self.root / "backups" / backup["file_name"]
        self.assertTrue(backup_path.is_file())
        connection = sqlite3.connect(backup_path)
        try:
            self.assertEqual("ok", connection.execute("PRAGMA quick_check").fetchone()[0])
        finally:
            connection.close()

    def test_wecom_user_policy_is_applied_without_bridge_restart(self) -> None:
        self.store.record_event(
            "message.received",
            payload={
                "connection_id": "primary",
                "message_id": "auth-message",
                "sender_id": "owner",
                "chat_id": "owner",
                "chat_type": "single",
            },
        )
        user = self.store.list_page("users", page=1, page_size=20)["items"][0]
        self.assertTrue(
            self.store.authorize_wecom_user(
                "primary", "owner", bootstrap_allowed=True
            )[0]
        )
        self.store.update_wecom_user(
            user["id"], {"status": "DISABLED"}, "admin", "127.0.0.1"
        )
        self.assertFalse(
            self.store.authorize_wecom_user(
                "primary", "owner", bootstrap_allowed=True
            )[0]
        )
        self.store.update_wecom_user(
            user["id"], {"status": "ALLOWED"}, "admin", "127.0.0.1"
        )
        self.assertTrue(
            self.store.authorize_wecom_user(
                "primary", "owner", bootstrap_allowed=False
            )[0]
        )

    def test_control_commands_are_claimed_once_and_completed_by_owner(self) -> None:
        self.store.record_event(
            "task.started", trace_id="trace", payload={"task_id": "task"}
        )
        command = self.store.enqueue_task_cancel(
            "task", "cancel-task", actor_id="admin", ip="127.0.0.1"
        )
        duplicate = self.store.enqueue_task_cancel(
            "task", "cancel-task", actor_id="admin", ip="127.0.0.1"
        )
        self.assertEqual(command["id"], duplicate["id"])
        claimed = self.store.claim_control_commands(
            "bridge-1", command_types={"CANCEL_TASK"}
        )
        self.assertEqual([command["id"]], [item["id"] for item in claimed])
        self.assertEqual([], self.store.claim_control_commands("bridge-2"))
        self.assertFalse(
            self.store.complete_control_command(
                command["id"], success=True, worker_id="bridge-2"
            )
        )
        self.assertTrue(
            self.store.complete_control_command(
                command["id"],
                success=True,
                result={"stopped": True},
                worker_id="bridge-1",
            )
        )
        self.assertEqual("SUCCEEDED", self.store.get_control_command(command["id"])["status"])

    def test_stale_control_command_lease_is_recovered(self) -> None:
        command = self.store.enqueue_control_command(
            "RESTART_SERVICE", "service", "bridge", payload={},
            idempotency_key="restart-stale", actor_id="admin", ip="127.0.0.1",
        )
        self.assertEqual(1, len(self.store.claim_control_commands("dead-worker")))
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE control_commands SET claimed_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (command["id"],),
            )
        reclaimed = self.store.claim_control_commands(
            "replacement-worker", lease_seconds=30
        )
        self.assertEqual(command["id"], reclaimed[0]["id"])
        self.assertEqual("replacement-worker", reclaimed[0]["claimed_by"])

    def test_restart_reconciles_incomplete_tasks(self) -> None:
        self.store.record_event(
            "task.started", trace_id="trace", payload={"task_id": "running"}
        )
        AdminStore(
            self.root / "admin.db",
            SecretBox.load(self.root / "master.key"),
            reconcile_on_start=True,
        )
        self.assertEqual("INTERRUPTED", self.store.task_detail("running")["status"])

    def test_redaction_recurses_through_payload(self) -> None:
        redacted = redact_data(
            {"Authorization": "Bearer abcdefghijklmnop", "nested": {"password": "x"}}
        )
        self.assertEqual("***", redacted["Authorization"])
        self.assertEqual("***", redacted["nested"]["password"])


if __name__ == "__main__":
    unittest.main()
