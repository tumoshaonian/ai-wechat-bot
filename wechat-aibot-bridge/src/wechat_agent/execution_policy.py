"""Conservative tool dispatch policy, independent of application names."""
import hashlib
import json

# Only local UI observation and the separately validated confirmation handshake.
# Shell, filesystem, file delivery, UI mutation, subagents and unknown tools ask.
OBSERVATION_TOOLS = frozenset({"mcp__desktop__list_windows", "mcp__desktop__inspect_window"})
CONFIRMATION_TOOL = "mcp__desktop__request_confirmation"


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
