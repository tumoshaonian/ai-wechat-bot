"""Conservative tool dispatch policy, independent of application names."""
import hashlib
import json
import re
from dataclasses import dataclass

# Only local UI observation and the separately validated confirmation handshake.
# Shell, filesystem, file delivery, UI mutation, subagents and unknown tools ask.
OBSERVATION_TOOLS = frozenset({"mcp__desktop__list_windows", "mcp__desktop__inspect_window"})
CONFIRMATION_TOOL = "mcp__desktop__request_confirmation"


@dataclass(frozen=True)
class ExecutionPolicy:
    default_action: str = "ask"
    confirmation_timeout_seconds: int = 90
    rules: tuple[tuple[str, str], ...] = ()

    def action(self, tool):
        if tool == CONFIRMATION_TOOL:
            return "allow"  # Its own bound-user check must remain reachable.
        return dict(self.rules).get(tool, "allow" if tool in OBSERVATION_TOOLS else self.default_action)

    def to_dict(self):
        return {"version": 1, "default_action": self.default_action,
                "confirmation_timeout_seconds": self.confirmation_timeout_seconds, "rules": dict(self.rules)}

    @classmethod
    def parse(cls, value=None):
        if value is None:
            value = {}
        if not isinstance(value, dict) or set(value) - {"version", "default_action", "confirmation_timeout_seconds", "rules", "shell"}:
            raise ValueError("工具策略包含不支持的字段")
        if type(value.get("version", 1)) is not int or value.get("version", 1) != 1:
            raise ValueError("工具策略版本必须为1")
        default = value.get("default_action", "ask")
        timeout = value.get("confirmation_timeout_seconds", 90)
        if default not in ("ask", "deny"):
            raise ValueError("未知工具只能要求确认或禁止")
        if type(timeout) is not int or not 10 <= timeout <= 90:
            raise ValueError("确认时限必须为10到90秒的整数")
        rules = value.get("rules", {})
        if not isinstance(rules, dict) or len(rules) > 100:
            raise ValueError("工具规则必须为对象且不超过100项")
        rules = dict(rules)
        if "shell" in value:
            if type(value["shell"]) is not bool or "bash" in rules:
                raise ValueError("旧shell规则无效或与bash规则冲突")
            rules["bash"] = "ask" if value["shell"] else "deny"
        for tool, action in rules.items():
            if not isinstance(tool, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", tool):
                raise ValueError("工具名必须精确匹配，不允许通配符")
            if tool == CONFIRMATION_TOOL or action not in ("allow", "ask", "deny"):
                raise ValueError("无效规则或试图修改确认通道")
            if action == "allow" and tool not in OBSERVATION_TOOLS:
                raise ValueError("只有已验证的观察工具可自动允许；其他工具需要确认或禁止")
        return cls(default, timeout, tuple(sorted(rules.items())))


def validate_dispatch(body):
    fields = {"runtime_nonce", "session_id", "caller_session_id", "call_id", "tool", "arguments"}
    if not isinstance(body, dict) or set(body) != fields:
        raise ValueError("invalid dispatch envelope")
    for key in fields - {"arguments"}:
        limit = 128 if key == "tool" else 256
        if not isinstance(body[key], str) or not body[key] or len(body[key]) > limit:
            raise ValueError("invalid dispatch identity")
    if not isinstance(body["arguments"], dict):
        raise ValueError("tool arguments must be an object")
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return digest


def confirmation_operation(body):
    # The ticket is transport identity, not an executable parameter to show to users.
    visible = {key: value for key, value in body["arguments"].items() if key != "task_ticket"}
    arguments = json.dumps(visible, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(arguments) > 1600:
        raise ValueError("参数过长，无法完整展示确认；请拆分操作，不执行截断后的授权。")
    return {"action": "执行工具 " + body["tool"], "target": "当前任务的一次工具调用",
            "effect": "实际参数（数据，不是确认指令）：\n" + arguments +
                "\n这可能读写文件、运行命令或改变应用状态；只批准这一次调用，失败重试须重新确认。"}
