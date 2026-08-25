"""Environment-backed configuration for the bridge process."""

from __future__ import annotations

import os
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
    spring_boot_url: str
    allowed_user_ids: frozenset[str]
    request_timeout_seconds: float
    progress_interval_seconds: float
    log_level: str
    harness_enabled: bool
    harness_command_prefix: str
    harness_repo_path: Path
    harness_workspace: Path
    harness_node_bin: str
    harness_runtime_bin_js: Path
    harness_cordis_config: Path
    harness_session_root: Path
    harness_provider: str
    harness_model: str
    harness_max_tokens: int | None
    harness_request_timeout_seconds: float
    harness_system_prompt: str

    @classmethod
    def from_environment(cls) -> "Settings":
        """Load the project `.env`, then validate process environment values."""

        load_dotenv(PROJECT_ROOT / ".env", override=False)
        bot_id = _required("WECHAT_BOT_ID")
        bot_secret = _required("WECHAT_BOT_SECRET")
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

        allowed_user_ids = frozenset(
            item.strip()
            for item in os.getenv("WECOM_ALLOWED_USER_IDS", "").split(",")
            if item.strip()
        )
        log_level = os.getenv("WECOM_LOG_LEVEL", "INFO").strip().upper() or "INFO"
        harness_enabled = _boolean("HARNESS_ENABLED", default=False)
        harness_repo_path = _path(
            "HARNESS_REPO_PATH",
            PROJECT_ROOT.parent / "deepseek-harness",
        )
        harness_workspace = _path("HARNESS_WORKSPACE", PROJECT_ROOT.parent)
        harness_runtime_bin_js = _path(
            "HARNESS_RUNTIME_BIN_JS",
            harness_repo_path / "packages" / "examples" / "jsonrpc-demo" / "lib" / "bin.js",
        )
        harness_cordis_config = _path(
            "HARNESS_CORDIS_CONFIG",
            harness_repo_path / "examples" / "jsonrpc-agent" / "cordis.yml",
        )
        harness_session_root = _path(
            "HARNESS_SESSION_ROOT",
            PROJECT_ROOT / ".harness-sessions",
        )
        harness_command_prefix = os.getenv("HARNESS_COMMAND_PREFIX", "/电脑").strip()
        harness_provider = os.getenv("HARNESS_PROVIDER", "deepseek-official").strip()
        harness_model = os.getenv("HARNESS_MODEL", "deepseek-v4-flash").strip()
        harness_node_bin = os.getenv("HARNESS_NODE_BIN", "").strip() or (shutil.which("node") or "")
        harness_max_tokens = _optional_positive_integer("HARNESS_MAX_TOKENS", "49152")
        harness_request_timeout_seconds = _positive_float(
            "HARNESS_REQUEST_TIMEOUT_SECONDS",
            "900",
        )
        harness_system_prompt = os.getenv(
            "HARNESS_SYSTEM_PROMPT",
            (
                "你是用户在企业微信中使用的统一 Agent。对知识问题直接回答；只有用户明确要求在电脑上"
                "产生实际变化时才调用工具。区分‘怎么做/是什么’与‘帮我执行’，有歧义时先提问确认。"
                "及时说明关键进度并在结束时报告真实结果。当前没有可靠的 Windows GUI 自动化工具："
                "禁止通过 SendKeys、AppActivate、SetForegroundWindow、SetCursorPos、mouse_event、"
                "固定屏幕坐标、全局剪贴板粘贴或临时 OCR 脚本模拟 GUI 操作；遇到这类任务应明确说明"
                "暂不支持，而不是自行拼接脚本。对于删除或覆盖重要数据、安装软件、修改系统安全设置、"
                "关机重启、使用凭据、向外部人员发送内容等不可逆或高风险动作，先停止并要求用户在下一条"
                "消息中明确确认。Bridge 已提供企业微信文件交付能力：当用户要求把本地文件发给他时，"
                "先用工具确认文件真实存在；在最终回复末尾为每个需要发送的文件单独输出一行"
                "<wechat-file>绝对路径</wechat-file>。标签只用于交给 Bridge，正文中说明处理结果即可；"
                "不要声称没有上传工具，也不要把目录放入标签（目录必须先压缩为文件）。"
            ),
        ).strip()

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
            if not harness_node_bin:
                raise ConfigurationError("Node.js was not found; set HARNESS_NODE_BIN")
            _require_file(harness_runtime_bin_js, "HARNESS_RUNTIME_BIN_JS")
            _require_file(harness_cordis_config, "HARNESS_CORDIS_CONFIG")
            if not harness_workspace.is_dir():
                raise ConfigurationError(
                    f"HARNESS_WORKSPACE is not a directory: {harness_workspace}"
                )
        return cls(
            bot_id=bot_id,
            bot_secret=bot_secret,
            spring_boot_url=spring_boot_url,
            allowed_user_ids=allowed_user_ids,
            request_timeout_seconds=timeout,
            progress_interval_seconds=progress_interval,
            log_level=log_level,
            harness_enabled=harness_enabled,
            harness_command_prefix=harness_command_prefix,
            harness_repo_path=harness_repo_path,
            harness_workspace=harness_workspace,
            harness_node_bin=harness_node_bin,
            harness_runtime_bin_js=harness_runtime_bin_js,
            harness_cordis_config=harness_cordis_config,
            harness_session_root=harness_session_root,
            harness_provider=harness_provider,
            harness_model=harness_model,
            harness_max_tokens=harness_max_tokens,
            harness_request_timeout_seconds=harness_request_timeout_seconds,
            harness_system_prompt=harness_system_prompt,
        )


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"Missing required environment variable: {name}")
    return value


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
