"""SQLite connection and versioned schema for the local control plane."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 4


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Database schema {version} is newer than supported {SCHEMA_VERSION}"
                )
            if version < 2:
                _ensure_column(connection, "control_commands", "claimed_by", "TEXT")
                _ensure_column(connection, "control_commands", "claimed_at", "TEXT")
                _ensure_column(connection, "control_commands", "completed_at", "TEXT")
                _ensure_column(connection, "control_commands", "result_json", "TEXT NOT NULL DEFAULT '{}'")
                _ensure_column(connection, "control_commands", "error_message", "TEXT")
            if version < 4:
                _ensure_column(connection, "file_artifacts", "path_ciphertext", "TEXT")
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
        finally:
            connection.close()


def _ensure_column(
    connection: sqlite3.Connection, table: str, column: str, definition: str
) -> None:
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


SCHEMA = r"""
CREATE TABLE IF NOT EXISTS admin_users (
  id TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
  display_name TEXT NOT NULL, password_hash TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ACTIVE', failed_attempts INTEGER NOT NULL DEFAULT 0,
  locked_until TEXT, last_login_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roles (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, description TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS permissions (name TEXT PRIMARY KEY, description TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS role_permissions (
  role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
  permission_name TEXT NOT NULL REFERENCES permissions(name) ON DELETE CASCADE,
  PRIMARY KEY(role_id, permission_name)
);
CREATE TABLE IF NOT EXISTS admin_user_roles (
  user_id TEXT NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
  role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
  PRIMARY KEY(user_id, role_id)
);
CREATE TABLE IF NOT EXISTS login_sessions (
  id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
  user_id TEXT NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, csrf_hash TEXT, ip_address TEXT, user_agent TEXT,
  created_at TEXT NOT NULL, expires_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON login_sessions(user_id, expires_at);
CREATE TABLE IF NOT EXISTS processed_messages (
  connection_id TEXT NOT NULL, message_id TEXT NOT NULL, claimed_at TEXT NOT NULL,
  PRIMARY KEY(connection_id, message_id)
);
CREATE TABLE IF NOT EXISTS event_stream (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
  idempotency_key TEXT UNIQUE, event_type TEXT NOT NULL, occurred_at TEXT NOT NULL,
  trace_id TEXT, actor_type TEXT NOT NULL, actor_id TEXT,
  resource_type TEXT, resource_id TEXT, severity TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_trace ON event_stream(trace_id, seq);
CREATE INDEX IF NOT EXISTS idx_event_type ON event_stream(event_type, seq);
CREATE TABLE IF NOT EXISTS channel_connections (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, channel_type TEXT NOT NULL DEFAULT 'WECOM_AIBOT',
  bot_id TEXT UNIQUE, secret_ciphertext TEXT, secret_configured INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'DRAFT', is_active INTEGER NOT NULL DEFAULT 0,
  environment TEXT NOT NULL DEFAULT 'local', notes TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_connections_active ON channel_connections(is_active, deleted_at);
CREATE TABLE IF NOT EXISTS wecom_users (
  id TEXT PRIMARY KEY, connection_id TEXT NOT NULL, external_user_id TEXT NOT NULL,
  display_name TEXT, status TEXT NOT NULL DEFAULT 'PENDING', policy_json TEXT NOT NULL DEFAULT '{}',
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, message_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(connection_id, external_user_id)
);
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, connection_id TEXT NOT NULL, user_id TEXT,
  external_chat_id TEXT NOT NULL, chat_type TEXT NOT NULL DEFAULT 'single',
  status TEXT NOT NULL DEFAULT 'ACTIVE', message_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, last_message_at TEXT NOT NULL,
  UNIQUE(connection_id, external_chat_id, chat_type)
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, connection_id TEXT NOT NULL, conversation_id TEXT,
  user_id TEXT, external_message_id TEXT, direction TEXT NOT NULL,
  message_type TEXT NOT NULL DEFAULT 'text', content TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'RECEIVED', task_id TEXT, trace_id TEXT,
  error_code TEXT, created_at TEXT NOT NULL,
  UNIQUE(connection_id, external_message_id, direction)
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, created_at);
CREATE TABLE IF NOT EXISTS agent_tasks (
  id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, conversation_id TEXT, message_id TEXT,
  status TEXT NOT NULL, request_summary TEXT NOT NULL DEFAULT '',
  result_summary TEXT NOT NULL DEFAULT '', error_code TEXT, error_message TEXT,
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, duration_ms INTEGER,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON agent_tasks(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_tasks_trace ON agent_tasks(trace_id);
CREATE TABLE IF NOT EXISTS tool_calls (
  id TEXT PRIMARY KEY, task_id TEXT, trace_id TEXT, tool_name TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'agent', status TEXT NOT NULL,
  input_json TEXT NOT NULL DEFAULT '{}', output_json TEXT NOT NULL DEFAULT '{}',
  error_code TEXT, error_message TEXT, retry_count INTEGER NOT NULL DEFAULT 0,
  started_at TEXT NOT NULL, finished_at TEXT, duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tools_task ON tool_calls(task_id, started_at);
CREATE TABLE IF NOT EXISTS file_artifacts (
  id TEXT PRIMARY KEY, task_id TEXT, trace_id TEXT, name TEXT NOT NULL,
  path_redacted TEXT NOT NULL DEFAULT '', path_ciphertext TEXT,
  mime_type TEXT, size_bytes INTEGER,
  sha256 TEXT, kind TEXT NOT NULL DEFAULT 'file', status TEXT NOT NULL DEFAULT 'AVAILABLE',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS file_deliveries (
  id TEXT PRIMARY KEY, artifact_id TEXT, task_id TEXT, trace_id TEXT,
  status TEXT NOT NULL, media_id_masked TEXT, error_code TEXT, error_message TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS log_events (
  id TEXT PRIMARY KEY, event_seq INTEGER UNIQUE, level TEXT NOT NULL,
  service TEXT NOT NULL, event_name TEXT NOT NULL, message TEXT NOT NULL,
  trace_id TEXT, resource_type TEXT, resource_id TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_created ON log_events(created_at DESC);
CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY, actor_type TEXT NOT NULL, actor_id TEXT,
  action TEXT NOT NULL, resource_type TEXT, resource_id TEXT, result TEXT NOT NULL,
  changes_json TEXT NOT NULL DEFAULT '{}', ip_address TEXT, trace_id TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);
CREATE TABLE IF NOT EXISTS control_commands (
  id TEXT PRIMARY KEY, command_type TEXT NOT NULL, target_type TEXT NOT NULL,
  target_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
  payload_json TEXT NOT NULL DEFAULT '{}', requested_by TEXT,
  idempotency_key TEXT UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  claimed_by TEXT, claimed_at TEXT, completed_at TEXT,
  result_json TEXT NOT NULL DEFAULT '{}', error_message TEXT
);
CREATE TABLE IF NOT EXISTS idempotency_responses (
  scope TEXT NOT NULL, key TEXT NOT NULL, status_code INTEGER NOT NULL,
  response_json TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  PRIMARY KEY(scope, key)
);
CREATE TABLE IF NOT EXISTS config_profiles (
  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, description TEXT NOT NULL DEFAULT '',
  active_revision_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS config_revisions (
  id TEXT PRIMARY KEY, profile_id TEXT NOT NULL REFERENCES config_profiles(id) ON DELETE CASCADE,
  version INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'DRAFT',
  provider TEXT NOT NULL, model TEXT NOT NULL, system_prompt TEXT NOT NULL,
  request_timeout_seconds REAL NOT NULL, task_timeout_seconds REAL NOT NULL,
  tool_policy_json TEXT NOT NULL DEFAULT '{}', created_by TEXT,
  created_at TEXT NOT NULL, published_at TEXT,
  UNIQUE(profile_id, version)
);
CREATE INDEX IF NOT EXISTS idx_config_revision_profile ON config_revisions(profile_id, version DESC);
CREATE TABLE IF NOT EXISTS alerts (
  id TEXT PRIMARY KEY, alert_type TEXT NOT NULL, severity TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN', title TEXT NOT NULL, message TEXT NOT NULL,
  trace_id TEXT, resource_type TEXT, resource_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  acknowledged_by TEXT, acknowledged_at TEXT, resolved_by TEXT, resolved_at TEXT,
  resolution_note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts(status, created_at DESC);
CREATE TABLE IF NOT EXISTS computer_nodes (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, hostname TEXT,
  status TEXT NOT NULL, os_name TEXT, capabilities_json TEXT NOT NULL DEFAULT '{}',
  last_heartbeat_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS service_instances (
  id TEXT PRIMARY KEY, node_id TEXT, service_type TEXT NOT NULL,
  status TEXT NOT NULL, pid INTEGER, version TEXT, started_at TEXT,
  last_heartbeat_at TEXT, health_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
  UNIQUE(node_id, service_type)
);
CREATE TABLE IF NOT EXISTS system_settings (
  key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_by TEXT, updated_at TEXT NOT NULL
);
"""
