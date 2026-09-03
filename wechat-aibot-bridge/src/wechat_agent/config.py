"""Environment-backed configuration for the bridge process."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class ConfigurationError(RuntimeError):
    """Raised when required bridge configuration is absent or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated deployment settings."""

    bot_id: str
    bot_secret: str
    connection_id: str
    spring_boot_url: str
    allowed_user_ids: frozenset[str]
    request_timeout_seconds: float
    progress_interval_seconds: float
    task_timeout_seconds: float
    log_level: str
    harness_enabled: bool
    harness_command_prefix: str
    harness_workspace: Path
    harness_session_root: Path
    harness_dsh_home: Path
    harness_dsh_bin: Path | None
    harness_runtime_mode: str
    harness_profile: str
    harness_patch_files: tuple[Path, ...]
    harness_permission_mode: str
    harness_provider: str
    harness_model: str
    harness_reasoning_effort: str | None
    harness_max_tokens: int | None
    harness_initialize_timeout_seconds: float
    harness_request_timeout_seconds: float
    harness_shutdown_timeout_seconds: float
    harness_system_prompt: str
    desktop_tools_enabled: bool
    desktop_powershell_bin: str
    desktop_uia_script: Path
    desktop_action_timeout_seconds: float
    desktop_screenshot_directory: Path
    desktop_log_file: Path
    doubao_launch_path: Path | None
    bridge_shutdown_file: Path
    agent_config_revision_id: str | None

    @classmethod
    def from_environment(cls) -> "Settings":
        """Load the project `.env`, then validate process environment values."""

        load_dotenv(PROJECT_ROOT / ".env", override=False)
        active_agent_config = _admin_active_agent_config()
        active_connection = _admin_active_connection()
        if active_connection is not None:
            bot_id = active_connection["bot_id"]
            bot_secret = active_connection["secret"]
            connection_id = active_connection["id"]
        else:
            bot_id = _required("WECHAT_BOT_ID")
            bot_secret = _required("WECHAT_BOT_SECRET")
            connection_id = _environment_connection_id(bot_id)
        spring_boot_url = os.getenv(
            "SPRING_BOOT_URL",
            "http://127.0.0.1:8080/api/wechat/reply",
        ).strip()
        if not spring_boot_url.startswith(("http://", "https://")):
            raise ConfigurationError("SPRING_BOOT_URL must be an HTTP(S) URL")

        raw_timeout = os.getenv("WECOM_REQUEST_TIMEOUT_SECONDS", "60").strip()
        try:
            timeout = float(raw_timeout)
        except ValueError as exc:
            raise ConfigurationError("WECOM_REQUEST_TIMEOUT_SECONDS must be numeric") from exc
        if timeout <= 0:
            raise ConfigurationError("WECOM_REQUEST_TIMEOUT_SECONDS must be positive")
        progress_interval = _positive_float("WECOM_PROGRESS_INTERVAL_SECONDS", "30")
        task_timeout = (
            float(active_agent_config["task_timeout_seconds"])
            if active_agent_config is not None
            else _positive_float("WECOM_TASK_TIMEOUT_SECONDS", "480")
        )
        if task_timeout >= 590:
            raise ConfigurationError(
                "WECOM_TASK_TIMEOUT_SECONDS must be below 590 because WeCom streams expire after 10 minutes"
            )

        allowed_user_ids = frozenset(
            item.strip()
            for item in os.getenv("WECOM_ALLOWED_USER_IDS", "").split(",")
            if item.strip()
        )
        log_level = os.getenv("WECOM_LOG_LEVEL", "INFO").strip().upper() or "INFO"
        harness_enabled = _boolean("HARNESS_ENABLED", default=False)
        harness_workspace = _path("HARNESS_WORKSPACE", PROJECT_ROOT.parent)
        desktop_tools_enabled = _boolean("DESKTOP_TOOLS_ENABLED", default=os.name == "nt")
        harness_session_root = _path(
            "HARNESS_SESSION_ROOT",
            PROJECT_ROOT / ".harness-sessions",
        )
        harness_dsh_home = _path(
            "HARNESS_DSH_HOME",
            harness_session_root,
        )
        harness_dsh_bin = _optional_path("HARNESS_DSH_BIN")
        harness_runtime_mode = _choice(
            "HARNESS_RUNTIME_MODE",
            "exe",
            {"exe", "node"},
        )
        harness_profile = os.getenv("HARNESS_PROFILE", "sdk").strip() or "sdk"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", harness_profile):
            raise ConfigurationError(
                "HARNESS_PROFILE must contain only letters, digits, '.', '_' or '-'"
            )
        harness_patch_files = _path_list(
            "HARNESS_PATCH_FILES",
            (
                PROJECT_ROOT
                / "wechat-aibot-bridge"
                / "config"
                / "harness-wecom.patch.yml",
            ),
        )
        harness_permission_mode = _choice(
            "HARNESS_PERMISSION_MODE",
            "danger-full-access",
            {"read-only", "workspace-write", "danger-full-access"},
        )
        harness_command_prefix = os.getenv("HARNESS_COMMAND_PREFIX", "/电脑").strip()
        harness_provider = str(
            (active_agent_config or {}).get("provider")
            or os.getenv("HARNESS_PROVIDER", "deepseek-official")
        ).strip()
        harness_model = str(
            (active_agent_config or {}).get("model")
            or os.getenv("HARNESS_MODEL", "deepseek-v4-flash")
        ).strip()
        harness_reasoning_effort = (
            os.getenv("HARNESS_REASONING_EFFORT", "max").strip() or None
        )
        harness_max_tokens = _optional_positive_integer("HARNESS_MAX_TOKENS", "49152")
        harness_initialize_timeout_seconds = _positive_float(
            "HARNESS_INITIALIZE_TIMEOUT_SECONDS",
            "90",
        )
        configured_harness_request_timeout = (
            float(active_agent_config["request_timeout_seconds"])
            if active_agent_config is not None
            else _positive_float("HARNESS_REQUEST_TIMEOUT_SECONDS", "450")
        )
        # The WeCom stream must still have time to stop the Runtime and deliver
        # a final explanation. Older admin revisions used 900s and are safely
        # clamped instead of making an otherwise valid deployment unbootable.
        harness_request_timeout_seconds = min(
            configured_harness_request_timeout,
            max(1.0, task_timeout - 30.0),
        )
        harness_shutdown_timeout_seconds = _positive_float(
            "HARNESS_SHUTDOWN_TIMEOUT_SECONDS",
            "10",
        )
        harness_system_prompt = os.getenv(
            "HARNESS_SYSTEM_PROMPT",
            (
                "你是用户在企业微信中使用的统一 Agent。对知识问题直接回答；只有用户明确要求在电脑上"
                "产生实际变化时才调用工具。区分‘怎么做/是什么’与‘帮我执行’，有歧义时先提问确认。"
                "及时说明关键进度并在结束时报告真实结果。Windows 桌面操作必须优先调用"
                "mcp__desktop__* 结构化工具；禁止通过 bash 自行拼接 SendKeys、AppActivate、"
                "SetForegroundWindow、SetCursorPos、mouse_event、固定屏幕坐标、全局剪贴板或临时 OCR"
                "脚本。用户要求在豆包中提问时，调用 mcp__desktop__doubao_ask；只有工具返回 ok=true、"
                "submitted=true 才能声称已发送问题。用户要求截图时，把工具返回的 screenshot_path 放在"
                "最终回复末尾的 <wechat-file>绝对路径</wechat-file> 标签中。任何桌面工具超时或报错后，"
                "只允许再调用一次不同的诊断工具；不得重复调用已经超时的同一工具，必须停止并如实报告"
                "具体失败步骤。对于删除或覆盖重要数据、"
                "安装软件、修改系统安全设置、"
                "关机重启、使用凭据、向外部人员发送内容等不可逆或高风险动作，先停止并要求用户在下一条"
                "消息中明确确认。Bridge 已提供企业微信文件交付能力：当用户要求把本地文件发给他时，"
                "先用工具确认文件真实存在；在最终回复末尾为每个需要发送的文件单独输出一行"
                "<wechat-file>绝对路径</wechat-file>。标签只用于交给 Bridge，正文中说明处理结果即可；"
                "不要声称没有上传工具，也不要把目录放入标签（目录必须先压缩为文件）。"
            ),
        ).strip()
        if active_agent_config is not None:
            published_prompt = str(active_agent_config.get("system_prompt") or "").strip()
            if published_prompt:
                harness_system_prompt = published_prompt
        desktop_powershell_bin = os.getenv("DESKTOP_POWERSHELL_BIN", "").strip() or (
            shutil.which("powershell.exe") or shutil.which("powershell") or ""
        )
        desktop_uia_script = _path(
            "DESKTOP_UIA_SCRIPT",
            PROJECT_ROOT / "wechat-aibot-bridge" / "scripts" / "windows_uia.ps1",
        )
        desktop_action_timeout_seconds = _positive_float(
            "DESKTOP_ACTION_TIMEOUT_SECONDS",
            "180",
        )
        desktop_screenshot_directory = _path(
            "DESKTOP_SCREENSHOT_DIRECTORY",
            Path.home() / "Pictures" / "WeComAgent",
        )
        desktop_log_file = _path(
            "DESKTOP_LOG_FILE",
            PROJECT_ROOT / "desktop-worker.log",
        )
        doubao_launch_path = _optional_path("DOUBAO_LAUNCH_PATH") or _discover_doubao_launcher()
        bridge_shutdown_file = _path(
            "BRIDGE_SHUTDOWN_FILE",
            PROJECT_ROOT / ".runtime" / "supervisor" / "bridge.stop.request",
        )

        if harness_enabled:
            if not allowed_user_ids:
                raise ConfigurationError(
                    "HARNESS_ENABLED=true requires WECOM_ALLOWED_USER_IDS"
                )
            if not harness_command_prefix:
                raise ConfigurationError("HARNESS_COMMAND_PREFIX cannot be empty")
            if not harness_provider or not harness_model:
                raise ConfigurationError("HARNESS_PROVIDER and HARNESS_MODEL cannot be empty")
            if not os.getenv("DEEPSEEK_API_KEY", "").strip():
                raise ConfigurationError("HARNESS_ENABLED=true requires DEEPSEEK_API_KEY")
            for patch_file in harness_patch_files:
                _require_file(patch_file, "HARNESS_PATCH_FILES")
            _validate_harness_runtime(harness_dsh_bin, harness_runtime_mode)
            if not harness_workspace.is_dir():
                raise ConfigurationError(
                    f"HARNESS_WORKSPACE is not a directory: {harness_workspace}"
                )
            if desktop_tools_enabled:
                if os.name != "nt":
                    raise ConfigurationError("DESKTOP_TOOLS_ENABLED=true currently requires Windows")
                if not desktop_powershell_bin:
                    raise ConfigurationError(
                        "Windows PowerShell was not found; set DESKTOP_POWERSHELL_BIN"
                    )
                _require_file(desktop_uia_script, "DESKTOP_UIA_SCRIPT")
        return cls(
            bot_id=bot_id,
            bot_secret=bot_secret,
            connection_id=connection_id,
            spring_boot_url=spring_boot_url,
            allowed_user_ids=allowed_user_ids,
            request_timeout_seconds=timeout,
            progress_interval_seconds=progress_interval,
            task_timeout_seconds=task_timeout,
            log_level=log_level,
            harness_enabled=harness_enabled,
            harness_command_prefix=harness_command_prefix,
            harness_workspace=harness_workspace,
            harness_session_root=harness_session_root,
            harness_dsh_home=harness_dsh_home,
            harness_dsh_bin=harness_dsh_bin,
            harness_runtime_mode=harness_runtime_mode,
            harness_profile=harness_profile,
            harness_patch_files=harness_patch_files,
            harness_permission_mode=harness_permission_mode,
            harness_provider=harness_provider,
            harness_model=harness_model,
            harness_reasoning_effort=harness_reasoning_effort,
            harness_max_tokens=harness_max_tokens,
            harness_initialize_timeout_seconds=harness_initialize_timeout_seconds,
            harness_request_timeout_seconds=harness_request_timeout_seconds,
            harness_shutdown_timeout_seconds=harness_shutdown_timeout_seconds,
            harness_system_prompt=harness_system_prompt,
            desktop_tools_enabled=desktop_tools_enabled,
            desktop_powershell_bin=desktop_powershell_bin,
            desktop_uia_script=desktop_uia_script,
            desktop_action_timeout_seconds=desktop_action_timeout_seconds,
            desktop_screenshot_directory=desktop_screenshot_directory,
            desktop_log_file=desktop_log_file,
            doubao_launch_path=doubao_launch_path,
            bridge_shutdown_file=bridge_shutdown_file,
            agent_config_revision_id=(
                str(active_agent_config.get("id"))
                if active_agent_config is not None
                else None
            ),
        )


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"Missing required environment variable: {name}")
    return value


def _admin_active_connection() -> dict[str, str] | None:
    """Load the selected encrypted connection without exposing it through HTTP."""

    if not _boolean("ADMIN_MANAGED_CONNECTIONS", default=True):
        return None
    try:
        from .admin.events import get_event_recorder

        value = get_event_recorder().get_active_connection_credentials()
    except Exception:
        return None
    if not isinstance(value, dict):
        return None
    required = ("id", "bot_id", "secret")
    if not all(isinstance(value.get(key), str) and value[key].strip() for key in required):
        return None
    return {key: value[key].strip() for key in required}


def _admin_active_agent_config() -> dict[str, object] | None:
    if not _boolean("ADMIN_MANAGED_AGENT_CONFIG", default=True):
        return None
    try:
        from .admin.events import get_event_recorder

        value = get_event_recorder().get_active_runtime_config()
    except Exception:
        return None
    if not isinstance(value, dict):
        return None
    required = {
        "id",
        "provider",
        "model",
        "system_prompt",
        "request_timeout_seconds",
        "task_timeout_seconds",
    }
    if not required.issubset(value):
        return None
    return value


def _environment_connection_id(bot_id: str) -> str:
    digest = hashlib.sha256(bot_id.encode("utf-8")).hexdigest()[:20]
    return f"wecom-{digest}"


def _boolean(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def _path(name: str, default: Path) -> Path:
    raw = os.getenv(name, "").strip()
    return Path(raw).expanduser().resolve() if raw else default.resolve()


def _optional_path(name: str) -> Path | None:
    raw = os.getenv(name, "").strip()
    return Path(raw).expanduser().resolve() if raw else None


def _path_list(name: str, default: tuple[Path, ...]) -> tuple[Path, ...]:
    """Parse an ordered semicolon-separated path list without splitting drive letters."""

    raw = os.getenv(name, "").strip()
    values = tuple(part.strip() for part in raw.split(";") if part.strip()) if raw else ()
    if not values:
        return tuple(path.resolve() for path in default)
    return tuple(Path(value).expanduser().resolve() for value in values)


def _choice(name: str, default: str, allowed: set[str]) -> str:
    value = os.getenv(name, default).strip().lower() or default
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ConfigurationError(f"{name} must be one of: {choices}")
    return value


def _discover_doubao_launcher() -> Path | None:
    """Find a normal Doubao shortcut without scanning arbitrary user files."""

    candidates = [
        Path.home() / "Desktop" / "豆包.lnk",
        Path.home() / "Desktop" / "Doubao.lnk",
    ]
    public = os.getenv("PUBLIC", "").strip()
    if public:
        candidates.extend(
            [
                Path(public) / "Desktop" / "豆包.lnk",
                Path(public) / "Desktop" / "Doubao.lnk",
            ]
        )
    appdata = os.getenv("APPDATA", "").strip()
    if appdata:
        programs = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        candidates.extend((programs / name) for name in ("豆包.lnk", "Doubao.lnk"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _positive_float(name: str, default: str) -> float:
    raw = os.getenv(name, default).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be numeric") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return value


def _optional_positive_integer(name: str, default: str) -> int | None:
    raw = os.getenv(name, default).strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be positive")
    return value


def _require_file(path: Path, name: str) -> None:
    if not path.is_file():
        raise ConfigurationError(f"{name} does not exist: {path}")


def _validate_harness_runtime(dsh_bin: Path | None, runtime_mode: str) -> None:
    """Fail during Bridge boot when the matching dsh carrier is unavailable."""

    if dsh_bin is not None:
        _require_file(dsh_bin, "HARNESS_DSH_BIN")
        return
    try:
        from deepseek_harness_runtime import resolve_bundled_launch_args
    except ImportError as exc:
        raise ConfigurationError(
            "deepseek-harness-runtime-bin is not installed; reinstall the local Harness SDK"
        ) from exc
    try:
        resolve_bundled_launch_args(runtime_mode)
    except (FileNotFoundError, ValueError) as exc:
        raise ConfigurationError(
            "DeepSeek Harness runtime is unavailable for "
            f"HARNESS_RUNTIME_MODE={runtime_mode}: {exc}"
        ) from exc
