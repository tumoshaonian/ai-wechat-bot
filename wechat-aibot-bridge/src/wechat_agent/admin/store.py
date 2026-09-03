"""Transactional repositories and event projections for the admin control plane."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .database import Database
from .redaction import redact_data, redact_text
from .security import SecretBox, hash_password, token_hash, verify_password


ALL_PERMISSIONS = {
    "dashboard.read", "connections.read", "connections.write", "users.read",
    "users.write", "conversations.read", "tasks.read", "tasks.control",
    "artifacts.read", "artifacts.send", "logs.read", "audit.read", "settings.read", "settings.write",
    "configs.read", "configs.write", "configs.publish", "alerts.read", "alerts.write",
    "admins.read", "admins.write", "system.backup", "system.retention", "runtime.read",
    "runtime.control",
}
ROLE_PERMISSIONS = {
    "super_admin": ALL_PERMISSIONS,
    "operator": {"dashboard.read", "connections.read", "users.read", "conversations.read", "tasks.read", "tasks.control", "artifacts.read", "artifacts.send", "logs.read", "settings.read", "alerts.read", "alerts.write", "runtime.read", "runtime.control", "system.backup"},
    "bot_admin": {"dashboard.read", "connections.read", "connections.write", "users.read", "users.write", "conversations.read", "tasks.read", "artifacts.read", "artifacts.send", "settings.read", "configs.read", "configs.write", "configs.publish", "alerts.read", "runtime.read"},
    "auditor": {"dashboard.read", "connections.read", "users.read", "conversations.read", "tasks.read", "artifacts.read", "logs.read", "audit.read", "settings.read", "configs.read", "alerts.read", "runtime.read"},
    "viewer": {"dashboard.read", "connections.read", "tasks.read", "logs.read", "settings.read"},
}
_DUMMY_PASSWORD_HASH = hash_password("TimingOnlyPassword!937")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class AdminStore:
    def __init__(
        self,
        database_path: Path,
        secret_box: SecretBox,
        *,
        reconcile_on_start: bool = False,
    ) -> None:
        self.database = Database(database_path)
        self.secret_box = secret_box
        self.database.initialize()
        self._seed_rbac()
        if reconcile_on_start:
            self.reconcile_interrupted_tasks()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _seed_rbac(self) -> None:
        with self.transaction() as connection:
            for permission in sorted(ALL_PERMISSIONS):
                connection.execute(
                    "INSERT OR IGNORE INTO permissions(name,description) VALUES(?,?)",
                    (permission, permission),
                )
            for role, permissions in ROLE_PERMISSIONS.items():
                role_id = f"role:{role}"
                connection.execute(
                    "INSERT OR IGNORE INTO roles(id,name,description) VALUES(?,?,?)",
                    (role_id, role, role.replace("_", " ").title()),
                )
                for permission in permissions:
                    connection.execute(
                        "INSERT OR IGNORE INTO role_permissions(role_id,permission_name) VALUES(?,?)",
                        (role_id, permission),
                    )

    def reconcile_interrupted_tasks(self) -> int:
        """Close tasks left running by a previous process instance."""

        now = utcnow()
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE agent_tasks SET status='INTERRUPTED',error_code='PROCESS_RESTARTED',"
                "error_message='The worker process stopped before the task reached a terminal state',"
                "finished_at=?,updated_at=? WHERE status IN "
                "('RECEIVED','QUEUED','RUNNING','WAITING_CONFIRMATION','CANCEL_REQUESTED')",
                (now, now),
            )
            count = cursor.rowcount
            if count:
                self._insert_audit(
                    connection, "system", None, "tasks.reconcile", "task", None,
                    "SUCCESS", {"interrupted_count": count},
                )
        return count

    # Authentication and RBAC -------------------------------------------------
    def setup_required(self) -> bool:
        with self.database.connect() as connection:
            return connection.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0] == 0

    def bootstrap_admin(self, username: str, display_name: str, password_hash: str) -> dict[str, Any]:
        now = utcnow()
        user_id = str(uuid.uuid4())
        with self.transaction() as connection:
            if connection.execute("SELECT 1 FROM admin_users LIMIT 1").fetchone():
                raise ConflictError("SETUP_ALREADY_COMPLETED", "Administrator setup is already complete")
            connection.execute(
                "INSERT INTO admin_users(id,username,display_name,password_hash,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (user_id, username.strip(), display_name.strip() or username.strip(), password_hash, now, now),
            )
            connection.execute(
                "INSERT INTO admin_user_roles(user_id,role_id) VALUES(?,?)",
                (user_id, "role:super_admin"),
            )
            self._insert_audit(connection, "system", None, "admin.setup", "admin_user", user_id, "SUCCESS", {"username": username})
        return self.get_user(user_id)

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        now_dt = datetime.now(timezone.utc)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM admin_users WHERE username=? COLLATE NOCASE", (username.strip(),)
            ).fetchone()
            if row is None:
                # Keep unknown-user and wrong-password work factors comparable.
                verify_password(password, _DUMMY_PASSWORD_HASH)
                return None
            locked_until = parse_time(row["locked_until"]) if row["locked_until"] else None
            if locked_until and locked_until > now_dt:
                raise AuthenticationLockedError(row["locked_until"])
            if row["status"] != "ACTIVE" or not verify_password(password, row["password_hash"]):
                failures = int(row["failed_attempts"]) + 1
                lock = (now_dt + timedelta(minutes=5)).isoformat() if failures >= 5 else None
                connection.execute(
                    "UPDATE admin_users SET failed_attempts=?,locked_until=?,updated_at=? WHERE id=?",
                    (0 if lock else failures, lock, utcnow(), row["id"]),
                )
                self._insert_audit(connection, "admin", row["id"], "auth.login", "admin_user", row["id"], "FAILED", {})
                return None
            connection.execute(
                "UPDATE admin_users SET failed_attempts=0,locked_until=NULL,last_login_at=?,updated_at=? WHERE id=?",
                (utcnow(), utcnow(), row["id"]),
            )
            self._insert_audit(connection, "admin", row["id"], "auth.login", "admin_user", row["id"], "SUCCESS", {})
        return self.get_user(row["id"])

    def get_user(self, user_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id,username,display_name,status,last_login_at,created_at FROM admin_users WHERE id=?",
                (user_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("ADMIN_NOT_FOUND", "Administrator was not found")
            roles = [item[0] for item in connection.execute(
                "SELECT r.name FROM roles r JOIN admin_user_roles ur ON ur.role_id=r.id WHERE ur.user_id=? ORDER BY r.name",
                (user_id,),
            )]
            permissions = [item[0] for item in connection.execute(
                "SELECT DISTINCT rp.permission_name FROM role_permissions rp JOIN admin_user_roles ur ON ur.role_id=rp.role_id WHERE ur.user_id=? ORDER BY rp.permission_name",
                (user_id,),
            )]
        result = dict(row)
        result.update(roles=roles, permissions=permissions)
        return result

    def create_session(self, user_id: str, token: str, csrf: str | None, kind: str, expires_at: str, ip: str | None, user_agent: str | None) -> str:
        session_id = str(uuid.uuid4())
        now = utcnow()
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM login_sessions WHERE expires_at<?",
                ((datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),),
            )
            connection.execute(
                "INSERT INTO login_sessions(id,token_hash,user_id,kind,csrf_hash,ip_address,user_agent,created_at,expires_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (session_id, token_hash(token), user_id, kind, token_hash(csrf) if csrf else None, ip, redact_text(user_agent or "", max_length=512), now, expires_at, now),
            )
        return session_id

    def resolve_session(self, token: str) -> dict[str, Any] | None:
        now = utcnow()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM login_sessions WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?",
                (token_hash(token), now),
            ).fetchone()
            if row is None:
                return None
            connection.execute("UPDATE login_sessions SET last_seen_at=? WHERE id=?", (now, row["id"]))
        return {**dict(row), "user": self.get_user(row["user_id"])}

    def revoke_session(self, session_id: str, actor_id: str | None = None) -> None:
        with self.transaction() as connection:
            connection.execute("UPDATE login_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (utcnow(), session_id))
            self._insert_audit(connection, "admin", actor_id, "auth.logout", "login_session", session_id, "SUCCESS", {})

    # Event ingestion and durable idempotency ---------------------------------
    def claim_message(self, connection_id: str, message_id: str) -> bool:
        try:
            with self.transaction() as connection:
                connection.execute(
                    "INSERT INTO processed_messages(connection_id,message_id,claimed_at) VALUES(?,?,?)",
                    (connection_id or "default", message_id, utcnow()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def record_event(self, event_type: str, *, trace_id: str | None = None, actor_type: str = "system", actor_id: str | None = None, resource_type: str | None = None, resource_id: str | None = None, payload: dict[str, Any] | None = None, severity: str = "INFO", idempotency_key: str | None = None) -> str:
        event_id = str(uuid.uuid4())
        now = utcnow()
        safe_payload = redact_data(payload or {})
        persisted_payload = _redact_operational_paths(safe_payload)
        with self.transaction() as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO event_stream(event_id,idempotency_key,event_type,occurred_at,trace_id,actor_type,actor_id,resource_type,resource_id,severity,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (event_id, idempotency_key, event_type, now, trace_id, actor_type, actor_id, resource_type, resource_id, severity.upper(), _json(persisted_payload)),
                )
            except sqlite3.IntegrityError:
                if idempotency_key:
                    row = connection.execute("SELECT event_id FROM event_stream WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                    if row:
                        return row[0]
                raise
            projection_payload = (
                safe_payload
                if event_type.startswith("artifact.")
                else persisted_payload
            )
            self._project_event(connection, cursor.lastrowid, event_id, event_type, now, trace_id, projection_payload, severity)
        return event_id

    def _project_event(self, connection: sqlite3.Connection, seq: int, event_id: str, event_type: str, now: str, trace_id: str | None, payload: dict[str, Any], severity: str) -> None:
        connection_id = str(payload.get("connection_id") or "default")
        if event_type == "message.received":
            sender = str(payload.get("sender_id") or "unknown")
            chat_id = str(payload.get("chat_id") or sender)
            chat_type = str(payload.get("chat_type") or "single")
            user_id = _stable_id("user", connection_id, sender)
            conversation_id = _stable_id("conversation", connection_id, chat_type, chat_id)
            connection.execute(
                "INSERT INTO wecom_users(id,connection_id,external_user_id,first_seen_at,last_seen_at,message_count) VALUES(?,?,?,?,?,1) ON CONFLICT(connection_id,external_user_id) DO UPDATE SET last_seen_at=excluded.last_seen_at,message_count=message_count+1",
                (user_id, connection_id, sender, now, now),
            )
            connection.execute(
                "INSERT INTO conversations(id,connection_id,user_id,external_chat_id,chat_type,created_at,last_message_at,message_count) VALUES(?,?,?,?,?,?,?,1) ON CONFLICT(connection_id,external_chat_id,chat_type) DO UPDATE SET last_message_at=excluded.last_message_at,message_count=message_count+1",
                (conversation_id, connection_id, user_id, chat_id, chat_type, now, now),
            )
            message_id = str(payload.get("message_id") or event_id)
            connection.execute(
                "INSERT OR IGNORE INTO messages(id,connection_id,conversation_id,user_id,external_message_id,direction,content,status,task_id,trace_id,created_at) VALUES(?,?,?,?,?,'INBOUND',?,'RECEIVED',?,?,?)",
                (_stable_id("message", connection_id, message_id, "in"), connection_id, conversation_id, user_id, message_id, redact_text(str(payload.get("content") or "")), payload.get("task_id"), trace_id or payload.get("trace_id"), now),
            )
        elif event_type.startswith("message.outbound"):
            # An outbound update is a new message even when the SDK only exposes the
            # inbound request ID. Never collapse progress and final replies together.
            ext_id = str(
                payload.get("outbound_message_id")
                or payload.get("response_id")
                or event_id
            )
            chat_id = str(payload.get("chat_id") or payload.get("sender_id") or "unknown")
            chat_type = str(payload.get("chat_type") or "single")
            conversation_id = _stable_id("conversation", connection_id, chat_type, chat_id)
            connection.execute(
                "INSERT OR IGNORE INTO conversations(id,connection_id,external_chat_id,chat_type,created_at,last_message_at,message_count) VALUES(?,?,?,?,?,?,0)",
                (conversation_id, connection_id, chat_id, chat_type, now, now),
            )
            connection.execute(
                "INSERT OR IGNORE INTO messages(id,connection_id,conversation_id,external_message_id,direction,content,status,task_id,trace_id,created_at) VALUES(?,?,?,?, 'OUTBOUND',?,?,?,?,?)",
                (_stable_id("message", connection_id, ext_id, "out"), connection_id, conversation_id, ext_id, redact_text(str(payload.get("content") or payload.get("text") or "")), str(payload.get("status") or "SENT"), payload.get("task_id"), trace_id or payload.get("trace_id"), now),
            )
        elif event_type.startswith("task."):
            self._project_task(connection, event_type, now, trace_id, payload)
        elif event_type.startswith("tool."):
            self._project_tool(connection, event_type, now, trace_id, payload)
        elif event_type.startswith("artifact.delivery."):
            self._project_delivery(connection, event_type, now, trace_id, payload)
        elif event_type.startswith("artifact."):
            self._project_artifact(connection, event_type, now, trace_id, payload)
        elif event_type.startswith("connection."):
            self._project_connection(connection, event_type, now, payload)
        elif event_type.startswith("node."):
            self._project_node(connection, event_type, now, payload)
        elif event_type.startswith("service."):
            self._project_service(connection, event_type, now, payload)
        if event_type.startswith("log.") or event_type in {"agent.notification", "system.error"}:
            connection.execute(
                "INSERT INTO log_events(id,event_seq,level,service,event_name,message,trace_id,resource_type,resource_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), seq, severity.upper(), str(payload.get("service") or "bridge"), event_type, redact_text(str(payload.get("message") or payload.get("content") or event_type)), trace_id, payload.get("resource_type"), payload.get("resource_id"), now),
            )
        if event_type in {
            "task.failed", "task.timeout", "connection.failed", "system.error",
            "connection.authentication_failed", "artifact.delivery.failed",
        }:
            connection.execute(
                "INSERT OR IGNORE INTO alerts(id,alert_type,severity,status,title,message,trace_id,resource_type,resource_id,created_at,updated_at) VALUES(?,?,?,'OPEN',?,?,?,?,?,?,?)",
                (
                    _stable_id("alert", event_id), event_type,
                    "CRITICAL" if event_type in {"connection.failed", "connection.authentication_failed"} else "ERROR",
                    event_type.replace(".", " ").title(),
                    redact_text(str(payload.get("error") or payload.get("message") or event_type)),
                    trace_id, payload.get("resource_type"),
                    payload.get("resource_id") or payload.get("task_id"),
                    now, now,
                ),
            )

    def _project_task(self, connection: sqlite3.Connection, event_type: str, now: str, trace_id: str | None, payload: dict[str, Any]) -> None:
        task_id = str(payload.get("task_id") or payload.get("id") or trace_id or uuid.uuid4())
        trace = str(trace_id or payload.get("trace_id") or task_id)
        connection_id = str(payload.get("connection_id") or "default")
        chat_id = str(payload.get("chat_id") or payload.get("sender_id") or "unknown")
        chat_type = str(payload.get("chat_type") or "single")
        conversation_id = (
            _stable_id("conversation", connection_id, chat_type, chat_id)
            if chat_id != "unknown"
            else None
        )
        external_message_id = str(payload.get("message_id") or "")
        message_id = (
            _stable_id("message", connection_id, external_message_id, "in")
            if external_message_id
            else None
        )
        status_map = {"task.started": "RUNNING", "task.completed": "SUCCEEDED", "task.failed": "FAILED", "task.cancelled": "CANCELLED", "task.timeout": "TIMED_OUT", "task.progress": "RUNNING"}
        status = status_map.get(event_type, str(payload.get("status") or "RUNNING").upper())
        declared_state = str(payload.get("state") or payload.get("status") or "").upper()
        if event_type == "task.completed" and declared_state in {
            "PARTIAL_SUCCEEDED", "PARTIALLY_SUCCEEDED", "PARTIAL_SUCCESS"
        }:
            status = "PARTIAL_SUCCEEDED"
        started = now if event_type == "task.started" else payload.get("started_at")
        finished = now if status in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT", "PARTIAL_SUCCEEDED", "INTERRUPTED"} else None
        connection.execute(
            "INSERT INTO agent_tasks(id,trace_id,conversation_id,message_id,status,request_summary,result_summary,error_code,error_message,created_at,started_at,finished_at,duration_ms,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET conversation_id=COALESCE(agent_tasks.conversation_id,excluded.conversation_id),message_id=COALESCE(agent_tasks.message_id,excluded.message_id),status=excluded.status,result_summary=CASE WHEN excluded.result_summary<>'' THEN excluded.result_summary ELSE agent_tasks.result_summary END,error_code=COALESCE(excluded.error_code,agent_tasks.error_code),error_message=COALESCE(excluded.error_message,agent_tasks.error_message),started_at=COALESCE(agent_tasks.started_at,excluded.started_at),finished_at=COALESCE(excluded.finished_at,agent_tasks.finished_at),duration_ms=COALESCE(excluded.duration_ms,agent_tasks.duration_ms),updated_at=excluded.updated_at",
            (task_id, trace, conversation_id, message_id, status, redact_text(str(payload.get("content") or payload.get("request") or ""), max_length=4096), redact_text(str(payload.get("result") or payload.get("reply") or ""), max_length=8192), payload.get("error_code"), redact_text(str(payload.get("error") or ""), max_length=4096) or None, now, started, finished, payload.get("duration_ms"), now),
        )

    def _project_tool(self, connection: sqlite3.Connection, event_type: str, now: str, trace_id: str | None, payload: dict[str, Any]) -> None:
        call_id = str(payload.get("tool_call_id") or payload.get("call_id") or _stable_id("tool", trace_id or "", str(payload.get("tool_name") or payload.get("name") or "unknown"), str(payload.get("started_at") or now)))
        status = {"tool.started": "RUNNING", "tool.completed": "SUCCEEDED", "tool.failed": "FAILED"}.get(event_type, str(payload.get("status") or "RUNNING").upper())
        connection.execute(
            "INSERT INTO tool_calls(id,task_id,trace_id,tool_name,category,status,input_json,output_json,error_code,error_message,retry_count,started_at,finished_at,duration_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,output_json=excluded.output_json,error_code=excluded.error_code,error_message=excluded.error_message,finished_at=COALESCE(excluded.finished_at,tool_calls.finished_at),duration_ms=COALESCE(excluded.duration_ms,tool_calls.duration_ms)",
            (call_id, payload.get("task_id"), trace_id or payload.get("trace_id"), str(payload.get("tool_name") or payload.get("name") or "unknown"), str(payload.get("category") or "agent"), status, _json(redact_data(payload.get("input") or payload.get("arguments") or {})), _json(redact_data(payload.get("output") or payload.get("result") or {})), payload.get("error_code"), redact_text(str(payload.get("error") or "")) or None, int(payload.get("retry_count") or 0), str(payload.get("started_at") or now), now if status in {"SUCCEEDED", "FAILED"} else None, payload.get("duration_ms")),
        )

    def _project_artifact(self, connection: sqlite3.Connection, event_type: str, now: str, trace_id: str | None, payload: dict[str, Any]) -> None:
        artifact_id = str(payload.get("artifact_id") or _stable_id("artifact", trace_id or "", str(payload.get("path") or payload.get("name") or uuid.uuid4())))
        path = str(payload.get("path") or "")
        encrypted_path = (
            self.secret_box.encrypt(path, context=f"artifact:{artifact_id}")
            if path
            else None
        )
        connection.execute(
            "INSERT INTO file_artifacts(id,task_id,trace_id,name,path_redacted,path_ciphertext,mime_type,size_bytes,sha256,kind,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,path_redacted=CASE WHEN excluded.path_redacted<>'' THEN excluded.path_redacted ELSE file_artifacts.path_redacted END,path_ciphertext=COALESCE(excluded.path_ciphertext,file_artifacts.path_ciphertext),size_bytes=COALESCE(excluded.size_bytes,file_artifacts.size_bytes),sha256=COALESCE(excluded.sha256,file_artifacts.sha256)",
            (artifact_id, payload.get("task_id"), trace_id or payload.get("trace_id"), str(payload.get("name") or Path(path).name or "artifact"), _redact_path(path), encrypted_path, payload.get("mime_type"), payload.get("size_bytes"), payload.get("sha256"), str(payload.get("kind") or "file"), str(payload.get("status") or "AVAILABLE"), now),
        )

    def _project_delivery(self, connection: sqlite3.Connection, event_type: str, now: str, trace_id: str | None, payload: dict[str, Any]) -> None:
        delivery_id = str(payload.get("delivery_id") or _stable_id("delivery", trace_id or "", str(payload.get("artifact_id") or payload.get("path") or uuid.uuid4())))
        status = {"artifact.delivery.started": "UPLOADING", "artifact.delivery.succeeded": "SENT", "artifact.delivery.failed": "FAILED"}.get(event_type, str(payload.get("status") or "PENDING").upper())
        connection.execute(
            "INSERT INTO file_deliveries(id,artifact_id,task_id,trace_id,status,media_id_masked,error_code,error_message,retry_count,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,media_id_masked=excluded.media_id_masked,error_code=excluded.error_code,error_message=excluded.error_message,retry_count=excluded.retry_count,updated_at=excluded.updated_at",
            (delivery_id, payload.get("artifact_id"), payload.get("task_id"), trace_id or payload.get("trace_id"), status, _mask(str(payload.get("media_id") or "")) or None, payload.get("error_code"), redact_text(str(payload.get("error") or "")) or None, int(payload.get("retry_count") or 0), now, now),
        )

    def _project_connection(
        self,
        connection: sqlite3.Connection,
        event_type: str,
        now: str,
        payload: dict[str, Any],
    ) -> None:
        connection_id = str(payload.get("connection_id") or "default")
        status_map = {
            "connection.connecting": "CONNECTING",
            "connection.authenticated": "ONLINE",
            "connection.online": "ONLINE",
            "connection.heartbeat": "ONLINE",
            "connection.degraded": "DEGRADED",
            "connection.error": "DEGRADED",
            "connection.authentication_failed": "FAILED",
            "connection.reconnecting": "RECONNECTING",
            "connection.disconnected": "DISABLED",
            "connection.failed": "FAILED",
            "connection.stopped": "DISABLED",
        }
        runtime_status = status_map.get(
            event_type, str(payload.get("status") or "READY").upper()
        )
        connection.execute(
            "INSERT INTO channel_connections(id,name,channel_type,bot_id,status,is_active,created_at,updated_at) "
            "VALUES(?,?, 'WECOM_AIBOT', ?, ?, 0, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at,"
            "bot_id=COALESCE(channel_connections.bot_id,excluded.bot_id)",
            (
                connection_id,
                str(payload.get("connection_name") or f"Discovered {connection_id}"),
                payload.get("bot_id"),
                runtime_status,
                now,
                now,
            ),
        )
        pending_row = connection.execute(
            "SELECT value_json FROM system_settings WHERE key='pending_connection_activation'"
        ).fetchone()
        if pending_row is None:
            return
        pending = _loads(pending_row["value_json"])
        if pending.get("candidate_connection_id") != connection_id:
            return
        if event_type in {"connection.authenticated", "connection.online"}:
            confirmed = {
                **pending,
                "state": "ACTIVE",
                "authenticated_at": now,
            }
            connection.execute(
                "DELETE FROM system_settings WHERE key='pending_connection_activation'"
            )
            connection.execute(
                "INSERT INTO system_settings(key,value_json,updated_by,updated_at) "
                "VALUES('last_connection_activation',?,NULL,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,"
                "updated_by=NULL,updated_at=excluded.updated_at",
                (_json(confirmed), now),
            )
            self._insert_audit(
                connection,
                "system",
                None,
                "connection.activation.confirmed",
                "connection",
                connection_id,
                "SUCCESS",
                {"activation_id": pending.get("activation_id")},
            )
        elif event_type == "connection.authentication_failed" or (
            event_type == "connection.error" and _looks_like_auth_failure(payload)
        ):
            self._rollback_connection_activation_in_transaction(
                connection,
                connection_id,
                reason=str(
                    payload.get("error")
                    or payload.get("message")
                    or "Runtime authentication failed"
                ),
                actor_id=None,
                ip=None,
                expected_activation_id=str(pending.get("activation_id") or ""),
            )

    def _project_node(self, connection: sqlite3.Connection, event_type: str, now: str, payload: dict[str, Any]) -> None:
        node_id = str(payload.get("node_id") or "local")
        state = str(payload.get("status") or ("ONLINE" if event_type in {"node.heartbeat", "node.online"} else "OFFLINE")).upper()
        connection.execute(
            "INSERT INTO computer_nodes(id,name,hostname,status,os_name,capabilities_json,last_heartbeat_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,hostname=COALESCE(excluded.hostname,computer_nodes.hostname),status=excluded.status,os_name=COALESCE(excluded.os_name,computer_nodes.os_name),capabilities_json=excluded.capabilities_json,last_heartbeat_at=excluded.last_heartbeat_at,updated_at=excluded.updated_at",
            (node_id, str(payload.get("name") or node_id), payload.get("hostname"), state, payload.get("os_name"), _json(redact_data(payload.get("capabilities") or {})), now, now, now),
        )

    def _project_service(self, connection: sqlite3.Connection, event_type: str, now: str, payload: dict[str, Any]) -> None:
        node_id = str(payload.get("node_id") or "local")
        service_type = str(payload.get("service_type") or payload.get("service") or "unknown")
        service_id = str(payload.get("service_id") or _stable_id("service", node_id, service_type))
        state = str(payload.get("status") or ("HEALTHY" if event_type in {"service.heartbeat", "service.healthy"} else "UNHEALTHY")).upper()
        connection.execute(
            "INSERT OR IGNORE INTO computer_nodes(id,name,status,capabilities_json,last_heartbeat_at,created_at,updated_at) VALUES(?,?,'ONLINE','{}',?,?,?)",
            (node_id, node_id, now, now, now),
        )
        connection.execute(
            "INSERT INTO service_instances(id,node_id,service_type,status,pid,version,started_at,last_heartbeat_at,health_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(node_id,service_type) DO UPDATE SET status=excluded.status,pid=excluded.pid,version=COALESCE(excluded.version,service_instances.version),started_at=COALESCE(excluded.started_at,service_instances.started_at),last_heartbeat_at=excluded.last_heartbeat_at,health_json=excluded.health_json,updated_at=excluded.updated_at",
            (service_id, node_id, service_type, state, payload.get("pid"), payload.get("version"), payload.get("started_at"), now, _json(redact_data(payload.get("health") or {})), now),
        )

    # CRUD and query surface --------------------------------------------------
    def list_page(self, table: str, *, page: int, page_size: int, q: str | None = None, status: str | None = None, connection_id: str | None = None, trace_id: str | None = None) -> dict[str, Any]:
        specifications = {
            "connections": ("channel_connections", "name || ' ' || COALESCE(bot_id,'') || ' ' || notes", "updated_at", "deleted_at IS NULL"),
            "users": ("wecom_users", "external_user_id || ' ' || COALESCE(display_name,'')", "last_seen_at", "1=1"),
            "conversations": ("conversations", "external_chat_id", "last_message_at", "1=1"),
            "tasks": ("agent_tasks", "request_summary || ' ' || result_summary || ' ' || COALESCE(error_message,'')", "updated_at", "1=1"),
            "tool_calls": ("tool_calls", "tool_name || ' ' || COALESCE(error_message,'')", "started_at", "1=1"),
            "artifacts": ("file_artifacts", "name || ' ' || path_redacted", "created_at", "1=1"),
            "deliveries": ("file_deliveries", "COALESCE(error_message,'')", "updated_at", "1=1"),
            "logs": ("log_events", "message || ' ' || event_name || ' ' || service", "created_at", "1=1"),
            "audit": ("audit_events", "action || ' ' || COALESCE(resource_id,'')", "created_at", "1=1"),
            "alerts": ("alerts", "title || ' ' || message || ' ' || alert_type", "created_at", "1=1"),
            "nodes": ("computer_nodes", "name || ' ' || COALESCE(hostname,'')", "updated_at", "1=1"),
            "services": ("service_instances", "service_type || ' ' || COALESCE(version,'')", "updated_at", "1=1"),
            "config_profiles": ("config_profiles", "name || ' ' || description", "updated_at", "1=1"),
            "admin_users": ("admin_users", "username || ' ' || display_name", "updated_at", "1=1"),
        }
        if table not in specifications:
            raise ValueError(f"Unsupported list: {table}")
        source, search_expr, order, base = specifications[table]
        clauses, parameters = [base], []
        if q:
            clauses.append(f"({search_expr}) LIKE ? ESCAPE '\\'")
            parameters.append("%" + _escape_like(q) + "%")
        columns = self._columns(source)
        status_column = (
            "status" if "status" in columns else "level" if table == "logs" else "result" if table == "audit" else None
        )
        if status and status_column:
            clauses.append(f"{status_column}=?")
            parameters.append(status.upper())
        if connection_id and "connection_id" in columns:
            clauses.append("connection_id=?")
            parameters.append(connection_id)
        if trace_id and "trace_id" in columns:
            clauses.append("trace_id=?")
            parameters.append(trace_id)
        where = " AND ".join(clauses)
        with self.database.connect() as connection:
            total = connection.execute(f"SELECT COUNT(*) FROM {source} WHERE {where}", parameters).fetchone()[0]
            rows = connection.execute(
                f"SELECT * FROM {source} WHERE {where} ORDER BY {order} DESC LIMIT ? OFFSET ?",
                (*parameters, page_size, (page - 1) * page_size),
            ).fetchall()
        return {"items": [_clean_row(dict(row)) for row in rows], "page": page, "page_size": page_size, "total": total}

    def _columns(self, table: str) -> set[str]:
        with self.database.connect() as connection:
            return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}

    def get_record(self, table: str, record_id: str) -> dict[str, Any]:
        sources = {"connections": "channel_connections", "users": "wecom_users", "conversations": "conversations", "tasks": "agent_tasks", "config_profiles": "config_profiles", "alerts": "alerts", "nodes": "computer_nodes", "services": "service_instances"}
        source = sources[table]
        with self.database.connect() as connection:
            row = connection.execute(f"SELECT * FROM {source} WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFoundError("RESOURCE_NOT_FOUND", f"{table.rstrip('s').title()} was not found")
        return _clean_row(dict(row))

    def list_conversation_messages(self, conversation_id: str, page: int, page_size: int) -> dict[str, Any]:
        with self.database.connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
            rows = connection.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at ASC LIMIT ? OFFSET ?", (conversation_id, page_size, (page - 1) * page_size)).fetchall()
        return {"items": [_clean_row(dict(row)) for row in rows], "page": page, "page_size": page_size, "total": total}

    def task_detail(self, task_id: str) -> dict[str, Any]:
        task = self.get_record("tasks", task_id)
        with self.database.connect() as connection:
            task["tool_calls"] = [_clean_row(dict(row)) for row in connection.execute("SELECT * FROM tool_calls WHERE task_id=? ORDER BY started_at", (task_id,))]
            task["artifacts"] = [_clean_row(dict(row)) for row in connection.execute("SELECT * FROM file_artifacts WHERE task_id=? ORDER BY created_at", (task_id,))]
            task["deliveries"] = [_clean_row(dict(row)) for row in connection.execute("SELECT * FROM file_deliveries WHERE task_id=? ORDER BY created_at", (task_id,))]
            task["events"] = [self._event_row(row) for row in connection.execute("SELECT * FROM event_stream WHERE trace_id=? ORDER BY seq", (task["trace_id"],))]
        return task

    # Agent configuration versions -------------------------------------------
    def create_config_profile(self, name: str, description: str, actor_id: str, ip: str | None) -> dict[str, Any]:
        profile_id, now = str(uuid.uuid4()), utcnow()
        with self.transaction() as connection:
            try:
                connection.execute("INSERT INTO config_profiles(id,name,description,created_at,updated_at) VALUES(?,?,?,?,?)", (profile_id, name.strip(), description, now, now))
            except sqlite3.IntegrityError as exc:
                raise ConflictError("CONFIG_NAME_EXISTS", "A config profile with this name already exists") from exc
            self._insert_audit(connection, "admin", actor_id, "config.profile.create", "config_profile", profile_id, "SUCCESS", {"name": name}, ip)
        return self.get_config_profile(profile_id)

    def update_config_profile(self, profile_id: str, changes: dict[str, Any], actor_id: str, ip: str | None) -> dict[str, Any]:
        updates = {key: value for key, value in changes.items() if key in {"name", "description"} and value is not None}
        if not updates:
            return self.get_config_profile(profile_id)
        assignments = ",".join(f"{key}=?" for key in updates)
        with self.transaction() as connection:
            try:
                cursor = connection.execute(f"UPDATE config_profiles SET {assignments},updated_at=? WHERE id=?", (*updates.values(), utcnow(), profile_id))
            except sqlite3.IntegrityError as exc:
                raise ConflictError("CONFIG_NAME_EXISTS", "A config profile with this name already exists") from exc
            if not cursor.rowcount:
                raise NotFoundError("CONFIG_NOT_FOUND", "Config profile was not found")
            self._insert_audit(connection, "admin", actor_id, "config.profile.update", "config_profile", profile_id, "SUCCESS", updates, ip)
        return self.get_config_profile(profile_id)

    def create_config_revision(self, profile_id: str, data: dict[str, Any], actor_id: str, ip: str | None) -> dict[str, Any]:
        revision_id, now = str(uuid.uuid4()), utcnow()
        with self.transaction() as connection:
            if not connection.execute("SELECT 1 FROM config_profiles WHERE id=?", (profile_id,)).fetchone():
                raise NotFoundError("CONFIG_NOT_FOUND", "Config profile was not found")
            version = connection.execute("SELECT COALESCE(MAX(version),0)+1 FROM config_revisions WHERE profile_id=?", (profile_id,)).fetchone()[0]
            connection.execute(
                "INSERT INTO config_revisions(id,profile_id,version,provider,model,system_prompt,request_timeout_seconds,task_timeout_seconds,tool_policy_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (revision_id, profile_id, version, data["provider"], data["model"], data["system_prompt"], data["request_timeout_seconds"], data["task_timeout_seconds"], _json(redact_data(data.get("tool_policy") or {})), actor_id, now),
            )
            connection.execute("UPDATE config_profiles SET updated_at=? WHERE id=?", (now, profile_id))
            self._insert_audit(connection, "admin", actor_id, "config.revision.create", "config_revision", revision_id, "SUCCESS", {"profile_id": profile_id, "version": version, "provider": data["provider"], "model": data["model"]}, ip)
        return self.get_config_revision(revision_id)

    def get_config_profile(self, profile_id: str) -> dict[str, Any]:
        profile = self.get_record("config_profiles", profile_id)
        with self.database.connect() as connection:
            profile["revisions"] = [_clean_row(dict(row)) for row in connection.execute("SELECT * FROM config_revisions WHERE profile_id=? ORDER BY version DESC", (profile_id,))]
        return profile

    def get_config_revision(self, revision_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM config_revisions WHERE id=?", (revision_id,)).fetchone()
        if row is None:
            raise NotFoundError("CONFIG_REVISION_NOT_FOUND", "Config revision was not found")
        return _clean_row(dict(row))

    def publish_config_revision(self, profile_id: str, revision_id: str, actor_id: str, ip: str | None) -> dict[str, Any]:
        now = utcnow()
        with self.transaction() as connection:
            row = connection.execute("SELECT id FROM config_revisions WHERE id=? AND profile_id=?", (revision_id, profile_id)).fetchone()
            if row is None:
                raise NotFoundError("CONFIG_REVISION_NOT_FOUND", "Config revision was not found")
            connection.execute("UPDATE config_revisions SET status='ARCHIVED' WHERE profile_id=? AND status='PUBLISHED'", (profile_id,))
            connection.execute("UPDATE config_revisions SET status='PUBLISHED',published_at=? WHERE id=?", (now, revision_id))
            connection.execute("UPDATE config_profiles SET active_revision_id=?,updated_at=? WHERE id=?", (revision_id, now, profile_id))
            connection.execute(
                "INSERT INTO system_settings(key,value_json,updated_by,updated_at) "
                "VALUES('active_agent_config',?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "value_json=excluded.value_json,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                (_json({"profile_id": profile_id, "revision_id": revision_id}), actor_id, now),
            )
            self._insert_audit(connection, "admin", actor_id, "config.publish", "config_revision", revision_id, "SUCCESS", {"profile_id": profile_id}, ip)
        result = self.get_config_revision(revision_id)
        result.update(needs_restart=True, activation_state="PUBLISHED")
        return result

    def get_active_runtime_config(self) -> dict[str, Any] | None:
        """Return the single globally selected immutable Agent revision."""

        with self.database.connect() as connection:
            setting = connection.execute(
                "SELECT value_json FROM system_settings WHERE key='active_agent_config'"
            ).fetchone()
            selected = _loads(setting["value_json"]) if setting else {}
            revision_id = (
                str(selected.get("revision_id") or "")
                if isinstance(selected, dict)
                else ""
            )
            row = None
            if revision_id:
                row = connection.execute(
                    "SELECT r.*,p.name AS profile_name FROM config_revisions r "
                    "JOIN config_profiles p ON p.id=r.profile_id "
                    "WHERE r.id=? AND p.active_revision_id=r.id",
                    (revision_id,),
                ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT r.*,p.name AS profile_name FROM config_revisions r "
                    "JOIN config_profiles p ON p.active_revision_id=r.id "
                    "ORDER BY r.published_at DESC,r.created_at DESC LIMIT 1"
                ).fetchone()
        return _clean_row(dict(row)) if row is not None else None

    def rollback_config(self, profile_id: str, target_revision_id: str, actor_id: str, ip: str | None) -> dict[str, Any]:
        target = self.get_config_revision(target_revision_id)
        if target["profile_id"] != profile_id:
            raise NotFoundError("CONFIG_REVISION_NOT_FOUND", "Config revision was not found in this profile")
        clone = self.create_config_revision(profile_id, {
            "provider": target["provider"], "model": target["model"],
            "system_prompt": target["system_prompt"],
            "request_timeout_seconds": target["request_timeout_seconds"],
            "task_timeout_seconds": target["task_timeout_seconds"],
            "tool_policy": target["tool_policy"],
        }, actor_id, ip)
        self.write_audit(actor_id, "config.rollback", "config_revision", clone["id"], "SUCCESS", {"source_revision_id": target_revision_id}, ip)
        return self.publish_config_revision(profile_id, clone["id"], actor_id, ip)

    # Alerts, administrators and operations ----------------------------------
    def update_alert(self, alert_id: str, action: str, note: str, actor_id: str, ip: str | None) -> dict[str, Any]:
        now = utcnow()
        if action == "acknowledge":
            assignments, values, allowed = "status='ACKNOWLEDGED',acknowledged_by=?,acknowledged_at=?,updated_at=?", (actor_id, now, now), {"OPEN", "ACKNOWLEDGED"}
        elif action == "resolve":
            assignments, values, allowed = "status='RESOLVED',resolved_by=?,resolved_at=?,resolution_note=?,updated_at=?", (actor_id, now, note, now), {"OPEN", "ACKNOWLEDGED", "RESOLVED"}
        else:
            raise ValueError("Unsupported alert action")
        with self.transaction() as connection:
            row = connection.execute("SELECT status FROM alerts WHERE id=?", (alert_id,)).fetchone()
            if row is None:
                raise NotFoundError("ALERT_NOT_FOUND", "Alert was not found")
            if row["status"] not in allowed:
                raise ConflictError("ALERT_STATE_CONFLICT", "Alert cannot transition from its current state")
            connection.execute(f"UPDATE alerts SET {assignments} WHERE id=?", (*values, alert_id))
            self._insert_audit(connection, "admin", actor_id, f"alert.{action}", "alert", alert_id, "SUCCESS", {"note": note}, ip)
        return self.get_record("alerts", alert_id)

    def list_roles(self) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            roles = []
            for row in connection.execute("SELECT id,name,description FROM roles ORDER BY name"):
                item = dict(row)
                item["permissions"] = [permission[0] for permission in connection.execute("SELECT permission_name FROM role_permissions WHERE role_id=? ORDER BY permission_name", (row["id"],))]
                roles.append(item)
        return roles

    def list_admin_users(self, page: int, page_size: int, q: str | None = None) -> dict[str, Any]:
        page_data = self.list_page("admin_users", page=page, page_size=page_size, q=q)
        for item in page_data["items"]:
            item.pop("password_hash", None)
            item["roles"] = self.get_user(item["id"])["roles"]
        return page_data

    def create_admin_user(self, username: str, display_name: str, password_hash: str, roles: list[str], actor_id: str, ip: str | None) -> dict[str, Any]:
        user_id, now = str(uuid.uuid4()), utcnow()
        with self.transaction() as connection:
            role_rows = self._validate_roles(connection, roles)
            try:
                connection.execute("INSERT INTO admin_users(id,username,display_name,password_hash,created_at,updated_at) VALUES(?,?,?,?,?,?)", (user_id, username, display_name or username, password_hash, now, now))
            except sqlite3.IntegrityError as exc:
                raise ConflictError("ADMIN_USERNAME_EXISTS", "Administrator username already exists") from exc
            for role_id in role_rows:
                connection.execute("INSERT INTO admin_user_roles(user_id,role_id) VALUES(?,?)", (user_id, role_id))
            self._insert_audit(connection, "admin", actor_id, "admin.create", "admin_user", user_id, "SUCCESS", {"username": username, "roles": roles}, ip)
        return self.get_user(user_id)

    def assign_roles(self, user_id: str, roles: list[str], actor_id: str, ip: str | None) -> dict[str, Any]:
        with self.transaction() as connection:
            if not connection.execute("SELECT 1 FROM admin_users WHERE id=?", (user_id,)).fetchone():
                raise NotFoundError("ADMIN_NOT_FOUND", "Administrator was not found")
            role_ids = self._validate_roles(connection, roles)
            has_super = connection.execute("SELECT 1 FROM admin_user_roles WHERE user_id=? AND role_id='role:super_admin'", (user_id,)).fetchone()
            if has_super and "role:super_admin" not in role_ids:
                super_count = connection.execute("SELECT COUNT(DISTINCT user_id) FROM admin_user_roles WHERE role_id='role:super_admin'").fetchone()[0]
                if super_count <= 1:
                    raise ConflictError("LAST_SUPER_ADMIN", "The last super administrator cannot lose that role")
            connection.execute("DELETE FROM admin_user_roles WHERE user_id=?", (user_id,))
            for role_id in role_ids:
                connection.execute("INSERT INTO admin_user_roles(user_id,role_id) VALUES(?,?)", (user_id, role_id))
            self._insert_audit(connection, "admin", actor_id, "admin.roles.assign", "admin_user", user_id, "SUCCESS", {"roles": roles}, ip)
        return self.get_user(user_id)

    def _validate_roles(self, connection: sqlite3.Connection, roles: list[str]) -> list[str]:
        normalized = sorted(set(roles))
        rows = connection.execute(f"SELECT id,name FROM roles WHERE name IN ({','.join('?' for _ in normalized)})", normalized).fetchall()
        if len(rows) != len(normalized):
            raise NotFoundError("ROLE_NOT_FOUND", "One or more roles were not found")
        return [row["id"] for row in rows]

    def backup_database(self, directory: Path, actor_id: str, ip: str | None) -> dict[str, Any]:
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = directory / f"admin-{timestamp}-{uuid.uuid4().hex[:8]}.db"
        with self.database.connect() as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target, pages=256)
            integrity = target.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"Backup integrity check failed: {integrity}")
        result = {"file_name": destination.name, "size_bytes": destination.stat().st_size, "created_at": utcnow(), "integrity": "ok"}
        self.write_audit(actor_id, "system.backup", "database", None, "SUCCESS", result, ip)
        return result

    def retention(self, *, event_days: int, log_days: int, session_days: int, audit_days: int, dry_run: bool, actor_id: str, ip: str | None) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        cutoffs = {
            "event_stream": ("occurred_at", (now - timedelta(days=event_days)).isoformat()),
            "log_events": ("created_at", (now - timedelta(days=log_days)).isoformat()),
            "login_sessions": ("expires_at", (now - timedelta(days=session_days)).isoformat()),
            "audit_events": ("created_at", (now - timedelta(days=audit_days)).isoformat()),
        }
        with self.transaction() as connection:
            counts = {table: connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {column}<?", (cutoff,)).fetchone()[0] for table, (column, cutoff) in cutoffs.items()}
            if not dry_run:
                # Child indexes/tables do not reference these append-only operational rows.
                for table, (column, cutoff) in cutoffs.items():
                    connection.execute(f"DELETE FROM {table} WHERE {column}<?", (cutoff,))
                self._insert_audit(connection, "admin", actor_id, "system.retention.run", "database", None, "SUCCESS", {"deleted": counts}, ip)
        return {"dry_run": dry_run, "eligible": counts, "total": sum(counts.values()), "evaluated_at": utcnow()}

    def dashboard(self) -> dict[str, Any]:
        today = datetime.now(timezone.utc).date().isoformat()
        with self.database.connect() as connection:
            task_counts = {row[0]: row[1] for row in connection.execute("SELECT status,COUNT(*) FROM agent_tasks WHERE created_at>=? GROUP BY status", (today,))}
            connection_row = connection.execute("SELECT id,name,status,is_active,updated_at FROM channel_connections WHERE deleted_at IS NULL AND is_active=1 LIMIT 1").fetchone()
            recent_failures = [dict(row) for row in connection.execute("SELECT id,trace_id,request_summary,error_code,error_message,updated_at FROM agent_tasks WHERE status IN ('FAILED','TIMED_OUT','INTERRUPTED') ORDER BY updated_at DESC LIMIT 5")]
            running = connection.execute("SELECT COUNT(*) FROM agent_tasks WHERE status IN ('RECEIVED','QUEUED','RUNNING','WAITING_CONFIRMATION','CANCEL_REQUESTED')").fetchone()[0]
            messages = connection.execute("SELECT COUNT(*) FROM messages WHERE created_at>=?", (today,)).fetchone()[0]
        return {"generated_at": utcnow(), "active_connection": dict(connection_row) if connection_row else None, "today": {"messages": messages, "tasks": sum(task_counts.values()), "by_status": task_counts}, "running_tasks": running, "recent_failures": recent_failures}

    def create_connection(self, data: dict[str, Any], actor_id: str, ip: str | None) -> dict[str, Any]:
        connection_id, now = str(uuid.uuid4()), utcnow()
        secret = str(data.pop("secret", "") or "")
        with self.transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO channel_connections(id,name,channel_type,bot_id,secret_ciphertext,secret_configured,status,environment,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (connection_id, data["name"].strip(), data.get("channel_type", "WECOM_AIBOT"), data.get("bot_id") or None, self.secret_box.encrypt(secret, context=connection_id) if secret else None, bool(secret), "DRAFT", data.get("environment", "local"), data.get("notes", ""), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("BOT_ID_EXISTS", "A connection with this Bot ID already exists") from exc
            self._insert_audit(connection, "admin", actor_id, "connection.create", "connection", connection_id, "SUCCESS", data, ip)
        return self.get_record("connections", connection_id)

    def get_active_connection_credentials(self) -> dict[str, str] | None:
        """Return decrypted credentials for the local Bridge only.

        This method is deliberately absent from the HTTP API and its return value must
        never be logged or serialized into events.
        """

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id,bot_id,secret_ciphertext FROM channel_connections "
                "WHERE is_active=1 AND deleted_at IS NULL ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        if row is None or not row["bot_id"] or not row["secret_ciphertext"]:
            return None
        return {
            "id": row["id"],
            "bot_id": row["bot_id"],
            "secret": self.secret_box.decrypt(
                row["secret_ciphertext"], context=row["id"]
            ),
        }

    def ensure_environment_connection(
        self, connection_id: str, bot_id: str, secret: str
    ) -> dict[str, str] | None:
        """One-time .env migration that never overwrites an active DB selection."""

        if not connection_id or not bot_id or not secret:
            return self.get_active_connection_credentials()
        now = utcnow()
        with self.transaction() as connection:
            active = connection.execute(
                "SELECT 1 FROM channel_connections WHERE is_active=1 AND deleted_at IS NULL LIMIT 1"
            ).fetchone()
            if active:
                return self.get_active_connection_credentials()
            existing = connection.execute(
                "SELECT id FROM channel_connections WHERE id=? OR bot_id=? ORDER BY id=? DESC LIMIT 1",
                (connection_id, bot_id, connection_id),
            ).fetchone()
            selected_id = existing["id"] if existing else connection_id
            envelope = self.secret_box.encrypt(secret, context=selected_id)
            if existing:
                connection.execute(
                    "UPDATE channel_connections SET name=CASE WHEN name='' THEN 'Environment WeCom Bot' ELSE name END,"
                    "bot_id=?,secret_ciphertext=?,secret_configured=1,status='READY',is_active=1,deleted_at=NULL,updated_at=? WHERE id=?",
                    (bot_id, envelope, now, selected_id),
                )
            else:
                connection.execute(
                    "INSERT INTO channel_connections(id,name,channel_type,bot_id,secret_ciphertext,secret_configured,status,is_active,environment,created_at,updated_at) VALUES(?,'Environment WeCom Bot','WECOM_AIBOT',?,?,1,'READY',1,'environment',?,?)",
                    (selected_id, bot_id, envelope, now, now),
                )
            self._insert_audit(
                connection, "system", None, "connection.environment_import",
                "connection", selected_id, "SUCCESS", {"bot_id_configured": True},
            )
        return self.get_active_connection_credentials()

    def update_connection(self, connection_id: str, data: dict[str, Any], expected_version: int, actor_id: str, ip: str | None) -> dict[str, Any]:
        allowed = {"name", "bot_id", "environment", "notes", "status"}
        updates = {key: value for key, value in data.items() if key in allowed and value is not None}
        secret = data.get("secret")
        assignments, parameters = [], []
        for key, value in updates.items():
            assignments.append(f"{key}=?")
            parameters.append(value)
        if secret:
            assignments.extend(["secret_ciphertext=?", "secret_configured=1"])
            parameters.append(self.secret_box.encrypt(str(secret), context=connection_id))
        assignments.extend(["version=version+1", "updated_at=?"])
        parameters.extend([utcnow(), connection_id, expected_version])
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT is_active FROM channel_connections WHERE id=? AND deleted_at IS NULL",
                (connection_id,),
            ).fetchone()
            if current is None:
                raise NotFoundError("CONNECTION_NOT_FOUND", "Connection was not found")
            if current["is_active"] and (secret is not None or "bot_id" in updates):
                raise ConflictError(
                    "ACTIVE_CONNECTION_CREDENTIAL_EDIT",
                    "Create and test a candidate connection instead of editing credentials used by the live Bridge",
                )
            cursor = connection.execute(f"UPDATE channel_connections SET {','.join(assignments)} WHERE id=? AND version=? AND deleted_at IS NULL", parameters)
            if not cursor.rowcount:
                exists = connection.execute("SELECT 1 FROM channel_connections WHERE id=? AND deleted_at IS NULL", (connection_id,)).fetchone()
                if not exists:
                    raise NotFoundError("CONNECTION_NOT_FOUND", "Connection was not found")
                raise ConflictError("VERSION_CONFLICT", "Connection was changed by another administrator")
            self._insert_audit(connection, "admin", actor_id, "connection.update", "connection", connection_id, "SUCCESS", updates, ip)
        return self.get_record("connections", connection_id)

    def delete_connection(self, connection_id: str, actor_id: str, ip: str | None) -> None:
        with self.transaction() as connection:
            row = connection.execute("SELECT is_active FROM channel_connections WHERE id=? AND deleted_at IS NULL", (connection_id,)).fetchone()
            if row is None:
                raise NotFoundError("CONNECTION_NOT_FOUND", "Connection was not found")
            if row["is_active"]:
                raise ConflictError("ACTIVE_CONNECTION", "Disable or switch the active connection before deletion")
            pending = connection.execute(
                "SELECT value_json FROM system_settings WHERE key='pending_connection_activation'"
            ).fetchone()
            activation = _loads(pending["value_json"]) if pending else {}
            if activation.get("previous_connection_id") == connection_id:
                raise ConflictError(
                    "CONNECTION_REQUIRED_FOR_ROLLBACK",
                    "This connection is retained until the pending activation is confirmed or rolled back",
                )
            connection.execute("UPDATE channel_connections SET deleted_at=?,updated_at=? WHERE id=?", (utcnow(), utcnow(), connection_id))
            self._insert_audit(connection, "admin", actor_id, "connection.delete", "connection", connection_id, "SUCCESS", {}, ip)

    def get_connection_probe_target(self, connection_id: str) -> dict[str, Any]:
        """Return credentials only to the in-process probe coordinator.

        This method must never be exposed directly by an HTTP route.  The returned
        version is checked again during activation, closing the gap where an admin
        could edit credentials while an older probe was still running.
        """

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id,bot_id,secret_ciphertext,secret_configured,is_active,status,version "
                "FROM channel_connections WHERE id=? AND deleted_at IS NULL",
                (connection_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("CONNECTION_NOT_FOUND", "Connection was not found")
        if not row["bot_id"] or not row["secret_configured"] or not row["secret_ciphertext"]:
            raise ConflictError(
                "CONNECTION_INCOMPLETE",
                "Bot ID and Secret are required before testing or activation",
            )
        try:
            secret = self.secret_box.decrypt(
                row["secret_ciphertext"], context=connection_id
            )
        except Exception as exc:
            raise ConflictError(
                "CONNECTION_SECRET_UNREADABLE",
                "The stored Secret cannot be decrypted",
            ) from exc
        if not secret:
            raise ConflictError(
                "CONNECTION_INCOMPLETE", "The stored Secret is empty"
            )
        return {
            "id": row["id"],
            "bot_id": row["bot_id"],
            "secret": secret,
            "is_active": bool(row["is_active"]),
            "status": row["status"],
            "version": int(row["version"]),
        }

    def record_connection_test(
        self,
        connection_id: str,
        result: dict[str, Any],
        actor_id: str,
        ip: str | None,
    ) -> None:
        """Persist only the probe's public, already credential-free result."""

        public = redact_data(result)
        with self.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM channel_connections WHERE id=? AND deleted_at IS NULL",
                (connection_id,),
            ).fetchone() is None:
                raise NotFoundError("CONNECTION_NOT_FOUND", "Connection was not found")
            self._insert_audit(
                connection,
                "admin",
                actor_id,
                "connection.test",
                "connection",
                connection_id,
                "SUCCESS" if bool(result.get("ok")) else "FAILED",
                public,
                ip,
            )

    def activate_connection_after_probe(
        self,
        connection_id: str,
        *,
        expected_version: int,
        probe_result: dict[str, Any],
        actor_id: str,
        ip: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Atomically select a verified candidate and enqueue its Bridge restart.

        Authentication has not completed on the real worker yet, so the candidate
        is explicitly ``ACTIVATING`` rather than ``ONLINE``.  A durable, secret-free
        rollback context is kept in ``system_settings`` until a runtime
        authentication event confirms or rejects the new connection.
        """

        if not probe_result.get("ok"):
            raise ConflictError(
                "LIVE_PROBE_REQUIRED",
                "A successful live authentication probe is required before activation",
            )
        now = utcnow()
        activation_id = str(uuid.uuid4())
        command_id = str(uuid.uuid4())
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT id,bot_id,secret_configured,is_active,status,version "
                "FROM channel_connections WHERE id=? AND deleted_at IS NULL",
                (connection_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("CONNECTION_NOT_FOUND", "Connection was not found")
            if row["is_active"]:
                existing = connection.execute(
                    "SELECT * FROM control_commands WHERE idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                selected = _clean_row(dict(row))
                selected.update(
                    activation_state="ALREADY_ACTIVE",
                    needs_restart=False,
                    previous_connection_id=connection_id,
                    control_command=_clean_row(dict(existing)) if existing else None,
                    message="This connection is already selected.",
                )
                return selected
            if int(row["version"]) != int(expected_version):
                raise ConflictError(
                    "CONNECTION_CHANGED_AFTER_PROBE",
                    "Connection credentials changed after the probe; test them again",
                )
            if not row["bot_id"] or not row["secret_configured"]:
                raise ConflictError(
                    "CONNECTION_INCOMPLETE",
                    "Bot ID and Secret are required before activation",
                )
            pending_activation = connection.execute(
                "SELECT value_json FROM system_settings WHERE key='pending_connection_activation'"
            ).fetchone()
            if pending_activation is not None:
                raise ConflictError(
                    "CONNECTION_ACTIVATION_IN_PROGRESS",
                    "Wait for the current connection activation to authenticate or roll back",
                )
            duplicate = connection.execute(
                "SELECT * FROM control_commands WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if duplicate is not None:
                result = _clean_row(dict(row))
                result.update(
                    activation_state="ACTIVATING",
                    needs_restart=True,
                    control_command=_clean_row(dict(duplicate)),
                    message="The verified connection switch is already pending.",
                )
                return result
            previous = connection.execute(
                "SELECT id FROM channel_connections WHERE is_active=1 AND deleted_at IS NULL LIMIT 1"
            ).fetchone()
            previous_id = str(previous["id"]) if previous else None
            payload = {
                "reason": "connection_activation",
                "activation_id": activation_id,
                "candidate_connection_id": connection_id,
                "previous_connection_id": previous_id,
                "candidate_version": expected_version,
                "verified_at": now,
                "success_criteria": {
                    "event": "connection.authenticated",
                    "connection_id": connection_id,
                },
                "failure_criteria": {
                    "event": "connection.authentication_failed",
                    "connection_id": connection_id,
                },
                "rollback": {
                    "enabled": bool(previous_id),
                    "previous_connection_id": previous_id,
                    "failed_connection_id": connection_id,
                },
            }
            connection.execute(
                "UPDATE channel_connections SET is_active=0,"
                "status=CASE WHEN status IN ('ONLINE','ACTIVATING') THEN 'READY' ELSE status END,"
                "updated_at=? WHERE is_active=1",
                (now,),
            )
            connection.execute(
                "UPDATE channel_connections SET is_active=1,status='ACTIVATING',"
                "version=version+1,updated_at=? WHERE id=?",
                (now, connection_id),
            )
            connection.execute(
                "INSERT INTO control_commands(id,command_type,target_type,target_id,payload_json,"
                "requested_by,idempotency_key,created_at,updated_at) "
                "VALUES(?,'RESTART_SERVICE','service','bridge',?,?,?,?,?)",
                (command_id, _json(payload), actor_id, idempotency_key, now, now),
            )
            pending = {**payload, "restart_command_id": command_id, "state": "PENDING_AUTHENTICATION"}
            connection.execute(
                "INSERT INTO system_settings(key,value_json,updated_by,updated_at) "
                "VALUES('pending_connection_activation',?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,"
                "updated_by=excluded.updated_by,updated_at=excluded.updated_at",
                (_json(pending), actor_id, now),
            )
            self._insert_audit(
                connection,
                "admin",
                actor_id,
                "connection.activate",
                "connection",
                connection_id,
                "ACCEPTED",
                {
                    "activation_id": activation_id,
                    "previous_connection_id": previous_id,
                    "command_id": command_id,
                    "probe": probe_result,
                },
                ip,
            )
        result = self.get_record("connections", connection_id)
        result.update(
            activation_state="PENDING_RESTART_AND_AUTHENTICATION",
            needs_restart=True,
            previous_connection_id=previous_id,
            activation_id=activation_id,
            control_command=self.get_control_command(command_id),
            message="Candidate authenticated in isolation. Bridge restart and live authentication are pending.",
        )
        return result

    def get_pending_connection_activation(self) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM system_settings WHERE key='pending_connection_activation'"
            ).fetchone()
        return _loads(row["value_json"]) if row else None

    def rollback_connection_activation(
        self,
        failed_connection_id: str,
        *,
        reason: str,
        actor_id: str | None = None,
        ip: str | None = None,
        expected_activation_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore the previous selection and enqueue one rollback restart."""

        with self.transaction() as connection:
            rollback = self._rollback_connection_activation_in_transaction(
                connection,
                failed_connection_id,
                reason=reason,
                actor_id=actor_id,
                ip=ip,
                expected_activation_id=expected_activation_id,
            )
        if rollback is None:
            raise ConflictError(
                "NO_PENDING_CONNECTION_ACTIVATION",
                "No matching connection activation is pending rollback",
            )
        return rollback

    def _rollback_connection_activation_in_transaction(
        self,
        connection: sqlite3.Connection,
        failed_connection_id: str,
        *,
        reason: str,
        actor_id: str | None,
        ip: str | None,
        expected_activation_id: str | None = None,
    ) -> dict[str, Any] | None:
        setting = connection.execute(
            "SELECT value_json FROM system_settings WHERE key='pending_connection_activation'"
        ).fetchone()
        if setting is None:
            return None
        pending = _loads(setting["value_json"])
        if pending.get("candidate_connection_id") != failed_connection_id:
            return None
        activation_id = str(pending.get("activation_id") or "")
        if expected_activation_id and activation_id != expected_activation_id:
            return None
        selected = connection.execute(
            "SELECT id FROM channel_connections WHERE id=? AND is_active=1 AND deleted_at IS NULL",
            (failed_connection_id,),
        ).fetchone()
        if selected is None:
            return None
        previous_id = pending.get("previous_connection_id")
        if previous_id:
            previous = connection.execute(
                "SELECT id FROM channel_connections WHERE id=? AND deleted_at IS NULL",
                (previous_id,),
            ).fetchone()
            if previous is None:
                previous_id = None
        now = utcnow()
        connection.execute(
            "UPDATE channel_connections SET is_active=0,status='FAILED',"
            "version=version+1,updated_at=? WHERE id=?",
            (now, failed_connection_id),
        )
        if previous_id:
            connection.execute(
                "UPDATE channel_connections SET is_active=1,status='READY',"
                "version=version+1,updated_at=? WHERE id=?",
                (now, previous_id),
            )
        command_id = str(uuid.uuid4())
        rollback_payload = {
            "reason": "connection_activation_rollback",
            "rollback_of_activation_id": activation_id,
            "restored_connection_id": previous_id,
            "failed_connection_id": failed_connection_id,
            "failure": redact_text(reason, max_length=1000),
            # Explicitly disable recursive rollback for the recovery restart.
            "rollback": {"enabled": False},
        }
        connection.execute(
            "INSERT OR IGNORE INTO control_commands(id,command_type,target_type,target_id,"
            "payload_json,requested_by,idempotency_key,created_at,updated_at) "
            "VALUES(?,'RESTART_SERVICE','service','bridge',?,?,?,?,?)",
            (
                command_id,
                _json(rollback_payload),
                actor_id,
                f"connection-rollback:{activation_id}",
                now,
                now,
            ),
        )
        existing = connection.execute(
            "SELECT id FROM control_commands WHERE idempotency_key=?",
            (f"connection-rollback:{activation_id}",),
        ).fetchone()
        command_id = str(existing["id"])
        history = {
            **pending,
            "state": "ROLLED_BACK",
            "rolled_back_at": now,
            "rollback_reason": redact_text(reason, max_length=1000),
            "rollback_command_id": command_id,
        }
        connection.execute(
            "DELETE FROM system_settings WHERE key='pending_connection_activation'"
        )
        connection.execute(
            "INSERT INTO system_settings(key,value_json,updated_by,updated_at) "
            "VALUES('last_connection_activation',?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,"
            "updated_by=excluded.updated_by,updated_at=excluded.updated_at",
            (_json(history), actor_id, now),
        )
        self._insert_audit(
            connection,
            "admin" if actor_id else "system",
            actor_id,
            "connection.activation.rollback",
            "connection",
            failed_connection_id,
            "SUCCESS",
            {
                "activation_id": activation_id,
                "restored_connection_id": previous_id,
                "rollback_command_id": command_id,
                "reason": reason,
            },
            ip,
        )
        return {
            "rolled_back": True,
            "activation_id": activation_id,
            "failed_connection_id": failed_connection_id,
            "restored_connection_id": previous_id,
            "control_command_id": command_id,
        }

    def update_wecom_user(self, user_id: str, changes: dict[str, Any], actor_id: str, ip: str | None) -> dict[str, Any]:
        allowed = {"display_name", "status", "policy"}
        updates = {k: v for k, v in changes.items() if k in allowed and v is not None}
        assignments, parameters = [], []
        for key, value in updates.items():
            column = "policy_json" if key == "policy" else key
            assignments.append(f"{column}=?")
            parameters.append(_json(redact_data(value)) if key == "policy" else value)
        if not assignments:
            return self.get_record("users", user_id)
        with self.transaction() as connection:
            cursor = connection.execute(f"UPDATE wecom_users SET {','.join(assignments)} WHERE id=?", (*parameters, user_id))
            if not cursor.rowcount:
                raise NotFoundError("USER_NOT_FOUND", "WeCom user was not found")
            self._insert_audit(connection, "admin", actor_id, "user.update", "wecom_user", user_id, "SUCCESS", updates, ip)
        return self.get_record("users", user_id)

    def authorize_wecom_user(
        self,
        connection_id: str,
        external_user_id: str,
        *,
        bootstrap_allowed: bool,
    ) -> tuple[bool, str]:
        """Resolve the live user decision without caching stale admin changes."""

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status,policy_json FROM wecom_users "
                "WHERE connection_id=? AND external_user_id=?",
                (connection_id, external_user_id),
            ).fetchone()
        if row is None:
            return bootstrap_allowed, "bootstrap_allowlist"
        status = str(row["status"] or "PENDING").upper()
        policy = _loads(row["policy_json"])
        if status == "DISABLED":
            return False, "user_disabled"
        if isinstance(policy, dict) and (
            policy.get("can_chat") is False
            or policy.get("chat_enabled") is False
        ):
            return False, "chat_disabled"
        if status in {"ALLOWED", "OBSERVE"}:
            return True, status.lower()
        return bootstrap_allowed, "pending_bootstrap_allowlist"

    def get_wecom_user_policy(
        self,
        connection_id: str,
        external_user_id: str,
    ) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT policy_json FROM wecom_users "
                "WHERE connection_id=? AND external_user_id=?",
                (connection_id, external_user_id),
            ).fetchone()
        policy = _loads(row["policy_json"]) if row is not None else {}
        return policy if isinstance(policy, dict) else {}

    def enqueue_task_cancel(self, task_id: str, idempotency_key: str, actor_id: str, ip: str | None) -> dict[str, Any]:
        now = utcnow()
        command_id = str(uuid.uuid4())
        with self.transaction() as connection:
            task = connection.execute("SELECT status FROM agent_tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise NotFoundError("TASK_NOT_FOUND", "Task was not found")
            if task["status"] not in {"RECEIVED", "QUEUED", "RUNNING", "WAITING_CONFIRMATION", "CANCEL_REQUESTED"}:
                raise ConflictError("TASK_NOT_RUNNING", "Only an active task can be cancelled")
            try:
                connection.execute("INSERT INTO control_commands(id,command_type,target_type,target_id,payload_json,requested_by,idempotency_key,created_at,updated_at) VALUES(?,'CANCEL_TASK','task',?,'{}',?,?,?,?)", (command_id, task_id, actor_id, idempotency_key, now, now))
            except sqlite3.IntegrityError:
                row = connection.execute("SELECT * FROM control_commands WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                return _clean_row(dict(row))
            connection.execute("UPDATE agent_tasks SET status='CANCEL_REQUESTED',updated_at=? WHERE id=?", (now, task_id))
            self._insert_audit(connection, "admin", actor_id, "task.cancel", "task", task_id, "SUCCESS", {"command_id": command_id}, ip)
        return {"id": command_id, "command_type": "CANCEL_TASK", "target_type": "task", "target_id": task_id, "status": "PENDING", "accepted": True, "idempotency_key": idempotency_key, "created_at": now, "updated_at": now}

    def enqueue_control_command(self, command_type: str, target_type: str, target_id: str, *, payload: dict[str, Any] | None, idempotency_key: str, actor_id: str, ip: str | None) -> dict[str, Any]:
        allowed = {
            "CANCEL_TASK", "END_SESSION", "RETRY_TASK", "RESTART_SERVICE",
            "START_SERVICE", "STOP_SERVICE", "RESEND_FILE",
        }
        command = command_type.upper()
        if command not in allowed:
            raise ValueError(f"Unsupported control command: {command}")
        now, command_id = utcnow(), str(uuid.uuid4())
        with self.transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO control_commands(id,command_type,target_type,target_id,payload_json,requested_by,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (command_id, command, target_type, target_id, _json(redact_data(payload or {})), actor_id, idempotency_key, now, now),
                )
            except sqlite3.IntegrityError:
                row = connection.execute("SELECT * FROM control_commands WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                return {**_clean_row(dict(row)), "accepted": True}
            self._insert_audit(connection, "admin", actor_id, command.lower(), target_type, target_id, "ACCEPTED", {"command_id": command_id}, ip)
        return {"id": command_id, "command_type": command, "target_type": target_type, "target_id": target_id, "status": "PENDING", "accepted": True, "payload": redact_data(payload or {}), "created_at": now, "updated_at": now}

    def enqueue_delivery_retry(
        self,
        delivery_id: str,
        idempotency_key: str,
        actor_id: str,
        ip: str | None,
    ) -> dict[str, Any]:
        """Create a new delivery attempt without exposing its local path."""

        now = utcnow()
        with self.transaction() as connection:
            previous_command = connection.execute(
                "SELECT * FROM control_commands WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if previous_command is not None:
                return {**_clean_row(dict(previous_command)), "accepted": True}
            source = connection.execute(
                "SELECT d.*,a.path_ciphertext,a.status AS artifact_status,a.task_id AS artifact_task_id,"
                "a.trace_id AS artifact_trace_id,t.conversation_id,c.connection_id "
                "FROM file_deliveries d JOIN file_artifacts a ON a.id=d.artifact_id "
                "LEFT JOIN agent_tasks t ON t.id=COALESCE(d.task_id,a.task_id) "
                "LEFT JOIN conversations c ON c.id=t.conversation_id WHERE d.id=?",
                (delivery_id,),
            ).fetchone()
            if source is None:
                raise NotFoundError("DELIVERY_NOT_FOUND", "File delivery was not found")
            if not source["path_ciphertext"]:
                raise ConflictError(
                    "ARTIFACT_PATH_UNAVAILABLE",
                    "This historical artifact has no recoverable local path",
                )
            if source["artifact_status"] != "AVAILABLE":
                raise ConflictError(
                    "ARTIFACT_UNAVAILABLE",
                    "The artifact is no longer available for delivery",
                )
            active = connection.execute(
                "SELECT id FROM channel_connections WHERE id=? AND is_active=1 "
                "AND deleted_at IS NULL",
                (source["connection_id"],),
            ).fetchone()
            if active is None:
                raise ConflictError(
                    "CONNECTION_NOT_ACTIVE",
                    "The artifact belongs to a WeCom connection that is not active",
                )
            artifact_id = str(source["artifact_id"])
            try:
                local_path = self.secret_box.decrypt(
                    source["path_ciphertext"], context=f"artifact:{artifact_id}"
                )
            except Exception as exc:
                raise ConflictError(
                    "ARTIFACT_PATH_UNREADABLE",
                    "The encrypted artifact path cannot be recovered",
                ) from exc
            resolved = Path(local_path).expanduser().resolve()
            if not resolved.is_file():
                raise ConflictError(
                    "ARTIFACT_FILE_MISSING",
                    "The local artifact no longer exists",
                )
            new_delivery_id = str(uuid.uuid4())
            retry_count = int(source["retry_count"] or 0) + 1
            connection.execute(
                "INSERT INTO file_deliveries(id,artifact_id,task_id,trace_id,status,retry_count,created_at,updated_at) "
                "VALUES(?,?,?,?, 'PENDING',?,?,?)",
                (
                    new_delivery_id,
                    artifact_id,
                    source["task_id"] or source["artifact_task_id"],
                    source["trace_id"] or source["artifact_trace_id"],
                    retry_count,
                    now,
                    now,
                ),
            )
            command_id = str(uuid.uuid4())
            payload = {
                "artifact_id": artifact_id,
                "source_delivery_id": delivery_id,
                "retry_count": retry_count,
            }
            connection.execute(
                "INSERT INTO control_commands(id,command_type,target_type,target_id,payload_json,requested_by,idempotency_key,created_at,updated_at) "
                "VALUES(?,'RESEND_FILE','delivery',?,?,?,?,?,?)",
                (
                    command_id,
                    new_delivery_id,
                    _json(payload),
                    actor_id,
                    idempotency_key,
                    now,
                    now,
                ),
            )
            self._insert_audit(
                connection,
                "admin",
                actor_id,
                "delivery.retry",
                "delivery",
                delivery_id,
                "ACCEPTED",
                {"command_id": command_id, "new_delivery_id": new_delivery_id},
                ip,
            )
        return {
            "id": command_id,
            "command_type": "RESEND_FILE",
            "target_type": "delivery",
            "target_id": new_delivery_id,
            "status": "PENDING",
            "accepted": True,
            "payload": payload,
            "created_at": now,
            "updated_at": now,
        }

    def get_delivery_retry_context(self, delivery_id: str) -> dict[str, Any]:
        """Return the decrypted delivery context exclusively to the Bridge worker."""

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT d.id,d.artifact_id,d.task_id,d.trace_id,d.retry_count,"
                "a.path_ciphertext,c.connection_id,c.external_chat_id,c.chat_type "
                "FROM file_deliveries d JOIN file_artifacts a ON a.id=d.artifact_id "
                "LEFT JOIN agent_tasks t ON t.id=COALESCE(d.task_id,a.task_id) "
                "LEFT JOIN conversations c ON c.id=t.conversation_id WHERE d.id=?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("DELIVERY_NOT_FOUND", "File delivery was not found")
            active = connection.execute(
                "SELECT 1 FROM channel_connections WHERE id=? AND is_active=1 "
                "AND deleted_at IS NULL",
                (row["connection_id"],),
            ).fetchone()
        if active is None:
            raise ConflictError(
                "CONNECTION_NOT_ACTIVE",
                "The delivery connection is no longer active",
            )
        if not row["path_ciphertext"]:
            raise ConflictError(
                "ARTIFACT_PATH_UNAVAILABLE",
                "The artifact path is unavailable",
            )
        path = self.secret_box.decrypt(
            row["path_ciphertext"], context=f"artifact:{row['artifact_id']}"
        )
        result = dict(row)
        result.pop("path_ciphertext", None)
        result["path"] = path
        return result

    def claim_control_commands(self, worker_id: str, *, command_types: set[str] | None = None, limit: int = 10, lease_seconds: int = 180) -> list[dict[str, Any]]:
        """Atomically lease pending commands to exactly one Bridge/Supervisor worker."""

        limit = max(1, min(limit, 100))
        with self.transaction() as connection:
            stale_before = (
                datetime.now(timezone.utc) - timedelta(seconds=max(30, lease_seconds))
            ).isoformat()
            connection.execute(
                "UPDATE control_commands SET status='PENDING',claimed_by=NULL,claimed_at=NULL,updated_at=? "
                "WHERE status='RUNNING' AND claimed_at<?",
                (utcnow(), stale_before),
            )
            clauses, params = ["status='PENDING'"], []
            if command_types:
                placeholders = ",".join("?" for _ in command_types)
                clauses.append(f"command_type IN ({placeholders})")
                params.extend(sorted(item.upper() for item in command_types))
            rows = connection.execute(
                f"SELECT id FROM control_commands WHERE {' AND '.join(clauses)} ORDER BY created_at LIMIT ?",
                (*params, limit),
            ).fetchall()
            ids = [row[0] for row in rows]
            if not ids:
                return []
            now = utcnow()
            placeholders = ",".join("?" for _ in ids)
            connection.execute(
                f"UPDATE control_commands SET status='RUNNING',claimed_by=?,claimed_at=?,updated_at=? WHERE status='PENDING' AND id IN ({placeholders})",
                (worker_id, now, now, *ids),
            )
            claimed = connection.execute(
                f"SELECT * FROM control_commands WHERE claimed_by=? AND claimed_at=? AND id IN ({placeholders}) ORDER BY created_at",
                (worker_id, now, *ids),
            ).fetchall()
        return [_clean_row(dict(row)) for row in claimed]

    def complete_control_command(self, command_id: str, *, success: bool, result: dict[str, Any] | None = None, error: str | None = None, worker_id: str | None = None) -> bool:
        now = utcnow()
        with self.transaction() as connection:
            command_row = connection.execute(
                "SELECT * FROM control_commands WHERE id=?", (command_id,)
            ).fetchone()
            parameters: list[Any] = ["SUCCEEDED" if success else "FAILED", _json(redact_data(result or {})), redact_text(error or "") or None, now, now, command_id]
            where = "id=? AND status='RUNNING'"
            if worker_id:
                where += " AND claimed_by=?"
                parameters.append(worker_id)
            cursor = connection.execute(
                f"UPDATE control_commands SET status=?,result_json=?,error_message=?,completed_at=?,updated_at=? WHERE {where}",
                parameters,
            )
            if cursor.rowcount:
                row = connection.execute("SELECT target_type,target_id,requested_by FROM control_commands WHERE id=?", (command_id,)).fetchone()
                self._insert_audit(connection, "system", worker_id, "control.complete", row["target_type"], row["target_id"], "SUCCESS" if success else "FAILED", {"command_id": command_id, "error": error})
                if not success and command_row is not None and command_row["command_type"] == "RESTART_SERVICE":
                    payload = _loads(command_row["payload_json"])
                    if payload.get("reason") == "connection_activation" and payload.get("rollback", {}).get("enabled"):
                        self._rollback_connection_activation_in_transaction(
                            connection,
                            str(payload.get("candidate_connection_id") or ""),
                            reason="Bridge restart failed before the candidate could authenticate",
                            actor_id=None,
                            ip=None,
                            expected_activation_id=str(payload.get("activation_id") or ""),
                        )
        return bool(cursor.rowcount)

    def release_control_commands(
        self,
        worker_id: str,
        *,
        command_types: set[str] | None = None,
    ) -> int:
        """Return this worker's unfinished leases to the queue during shutdown.

        A response/request already written to the Supervisor queue is intentionally
        left in place.  The replacement worker uses the durable command id to
        resume that exact request instead of issuing a second operation.
        """

        clauses = ["status='RUNNING'", "claimed_by=?"]
        parameters: list[Any] = [worker_id]
        if command_types:
            placeholders = ",".join("?" for _ in command_types)
            clauses.append(f"command_type IN ({placeholders})")
            parameters.extend(sorted(item.upper() for item in command_types))
        now = utcnow()
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE control_commands SET status='PENDING',claimed_by=NULL,"
                "claimed_at=NULL,updated_at=? WHERE " + " AND ".join(clauses),
                (now, *parameters),
            )
        return int(cursor.rowcount)

    def project_runtime_snapshot(
        self,
        node_payload: dict[str, Any],
        service_payloads: list[dict[str, Any]],
    ) -> None:
        """Refresh runtime read models without flooding the durable event log.

        Local Supervisor writes a status file every two seconds.  State-change
        events remain in ``event_stream`` for SSE/audit consumers, while this
        direct transactional projection keeps last-heartbeat values current.
        """

        now = utcnow()
        with self.transaction() as connection:
            self._project_node(connection, "node.heartbeat", now, node_payload)
            for payload in service_payloads:
                self._project_service(
                    connection, "service.heartbeat", now, payload
                )

    def fetch_events(self, after: int, limit: int = 200) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM event_stream WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)).fetchall()
        return [self._event_row(row) for row in rows]

    def get_control_command(self, command_id: str) -> dict[str, Any]:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM control_commands WHERE id=?", (command_id,)).fetchone()
        if row is None:
            raise NotFoundError("COMMAND_NOT_FOUND", "Control command was not found")
        return _clean_row(dict(row))

    def _event_row(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = _loads(result.pop("payload_json"))
        result.pop("idempotency_key", None)
        return result

    def write_audit(self, actor_id: str | None, action: str, resource_type: str | None, resource_id: str | None, result: str, changes: dict[str, Any], ip: str | None = None) -> None:
        with self.transaction() as connection:
            self._insert_audit(connection, "admin" if actor_id else "system", actor_id, action, resource_type, resource_id, result, changes, ip)

    def _insert_audit(self, connection: sqlite3.Connection, actor_type: str, actor_id: str | None, action: str, resource_type: str | None, resource_id: str | None, result: str, changes: dict[str, Any], ip: str | None = None) -> None:
        connection.execute(
            "INSERT INTO audit_events(id,actor_type,actor_id,action,resource_type,resource_id,result,changes_json,ip_address,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), actor_type, actor_id, action, resource_type, resource_id, result, _json(redact_data(changes)), ip, utcnow()),
        )


class StoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


class NotFoundError(StoreError):
    pass


class ConflictError(StoreError):
    pass


class AuthenticationLockedError(StoreError):
    def __init__(self, locked_until: str) -> None:
        super().__init__("LOGIN_LOCKED", "Too many failed login attempts")
        self.locked_until = locked_until


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None) -> Any:
    try:
        return json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}


def _clean_row(row: dict[str, Any]) -> dict[str, Any]:
    for key in [item for item in row if item.endswith("_ciphertext")]:
        row.pop(key, None)
    for key in list(row):
        if key.endswith("_json"):
            row[key[:-5]] = _loads(row.pop(key))
        elif key in {"secret_configured", "is_active"}:
            row[key] = bool(row[key])
    return row


def _stable_id(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, ":".join(parts)))


def _mask(value: str) -> str:
    if not value:
        return ""
    return value[:4] + "…" + value[-4:] if len(value) > 10 else "***"


def _redact_path(value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    return str(Path("…") / path.name) if path.is_absolute() else redact_text(value)


def _redact_operational_paths(value: Any, *, key_hint: str = "") -> Any:
    """Hide local absolute paths from queryable events and tool summaries."""

    if isinstance(value, dict):
        return {
            str(key): _redact_operational_paths(item, key_hint=str(key).lower())
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _redact_operational_paths(item, key_hint=key_hint)
            for item in value
        ]
    if isinstance(value, str) and key_hint in {
        "path",
        "paths",
        "file",
        "files",
        "cwd",
        "directory",
        "working_directory",
        "screenshot_path",
    }:
        path = Path(value)
        if path.is_absolute():
            return _redact_path(value)
    return value


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _looks_like_auth_failure(payload: dict[str, Any]) -> bool:
    code = str(payload.get("code") or payload.get("error_code") or "").upper()
    phase = str(payload.get("phase") or "").lower()
    message = str(payload.get("error") or payload.get("message") or "").lower()
    return phase == "authentication" or "AUTH" in code or any(
        marker in message
        for marker in ("authentication", "authenticate", "credential", "unauthorized")
    )
