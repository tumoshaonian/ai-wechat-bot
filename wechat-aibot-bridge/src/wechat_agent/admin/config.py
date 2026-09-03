"""Configuration for the independent administration service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True, slots=True)
class AdminSettings:
    database_path: Path
    master_key_path: Path
    host: str = "127.0.0.1"
    port: int = 8765
    cookie_name: str = "wecom_admin_session"
    cookie_secure: bool = False
    session_hours: int = 12
    sse_poll_seconds: float = 0.75
    static_directory: Path | None = None
    backup_directory: Path | None = None
    supervisor_runtime_dir: Path | None = None
    runtime_control_enabled: bool = True
    supervisor_poll_seconds: float = 0.75
    supervisor_command_timeout_seconds: float = 60.0
    supervisor_command_lease_seconds: int = 120
    supervisor_status_stale_seconds: float = 15.0
    connection_probe_timeout_seconds: float = 12.0

    @classmethod
    def from_environment(cls) -> "AdminSettings":
        # The desktop Supervisor starts the admin entry point directly.  Load the
        # shared project file here as well as in the Bridge so both processes use
        # the same database, encryption key and listen settings.  Explicit process
        # environment variables still win.
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        runtime = _path(
            "ADMIN_RUNTIME_DIR",
            PROJECT_ROOT / ".runtime" / "admin",
        )
        static_raw = os.getenv("ADMIN_STATIC_DIR", "").strip()
        static = (
            Path(static_raw).expanduser().resolve()
            if static_raw
            else Path(__file__).with_name("static").resolve()
        )
        host = os.getenv("ADMIN_HOST", "127.0.0.1").strip() or "127.0.0.1"
        port = _integer("ADMIN_PORT", 8765, minimum=1, maximum=65535)
        return cls(
            database_path=_path("ADMIN_DATABASE_PATH", runtime / "admin.db"),
            master_key_path=_path("ADMIN_MASTER_KEY_PATH", runtime / "master.key"),
            host=host,
            port=port,
            cookie_secure=_boolean("ADMIN_COOKIE_SECURE", False),
            session_hours=_integer("ADMIN_SESSION_HOURS", 12, minimum=1, maximum=168),
            static_directory=static,
            backup_directory=_path("ADMIN_BACKUP_DIR", runtime / "backups"),
            supervisor_runtime_dir=_path(
                "SUPERVISOR_RUNTIME_DIR",
                PROJECT_ROOT / ".runtime" / "supervisor",
            ),
            runtime_control_enabled=_boolean("ADMIN_RUNTIME_CONTROL_ENABLED", True),
            supervisor_poll_seconds=_float(
                "ADMIN_RUNTIME_POLL_SECONDS", 0.75, minimum=0.1, maximum=30.0
            ),
            supervisor_command_timeout_seconds=_float(
                "ADMIN_RUNTIME_COMMAND_TIMEOUT_SECONDS",
                60.0,
                minimum=2.0,
                maximum=300.0,
            ),
            supervisor_command_lease_seconds=_integer(
                "ADMIN_RUNTIME_COMMAND_LEASE_SECONDS",
                120,
                minimum=30,
                maximum=900,
            ),
            supervisor_status_stale_seconds=_float(
                "ADMIN_RUNTIME_STATUS_STALE_SECONDS",
                15.0,
                minimum=3.0,
                maximum=300.0,
            ),
            connection_probe_timeout_seconds=_float(
                "ADMIN_CONNECTION_PROBE_TIMEOUT_SECONDS",
                12.0,
                minimum=1.0,
                maximum=60.0,
            ),
        )


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _integer(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)).strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _path(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip()
    return Path(raw or default).expanduser().resolve()
