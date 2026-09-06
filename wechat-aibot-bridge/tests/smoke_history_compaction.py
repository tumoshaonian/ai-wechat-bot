"""Explicit real-model recovery smoke in an isolated journal and DSH_HOME."""

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from wechat_agent.adapters.deepseek_harness import DeepSeekHarnessBackend
from wechat_agent.config import Settings
from wechat_agent.conversation_journal import ConversationJournal
from wechat_agent.domain import IncomingMessage


async def main():
    settings = Settings.from_environment()
    with TemporaryDirectory(prefix="wecom-compaction-smoke-") as temporary:
        root = Path(temporary)
        settings = replace(settings, harness_session_root=root / "journal", harness_dsh_home=root / "home", harness_workspace=root, harness_recovery_max_bytes=8192)
        message = IncomingMessage("smoke", "test", "test", "single", "只读取已有对话，不调用工具：星桥项目的完整路径是什么？旧文件交付是成功还是未知？最后一轮的编号是多少？", task_id="smoke")
        journal = ConversationJournal(settings.harness_session_root)
        for i in range(14):
            journal.append(message.session_id, 1, "user", f"历史轮次编号{i}；项目 D:/sandbox/星桥计划，不要覆盖已有文件。" + "这些都是重复的历史背景，不是本轮待执行指令。" * 18)
            journal.append(message.session_id, 1, "assistant", {"text": "项目文档已准备，不能声称交付成功。"})
            journal.append(message.session_id, 1, "delivery", {"name": "星桥成果.zip", "status": "unknown", "meaning": "不能盲目重发"})
        backend = DeepSeekHarnessBackend(settings)
        try:
            result = await backend.reply(message)
            print(json.dumps({"reply": result.text}, ensure_ascii=False), flush=True)
            assert "D:/sandbox/星桥计划" in result.text
            assert "13" in result.text
            assert "未知" in result.text or "unknown" in result.text
            with closing(sqlite3.connect(journal.path)) as db:
                checkpoints = db.execute("SELECT count(*) FROM checkpoints").fetchone()[0]
                facts = db.execute("SELECT count(*) FROM facts").fetchone()[0]
            assert checkpoints > 0 and facts >= 42
            history, _ = journal.context(message.session_id, 1, 0, 8192)
            assert len(history.encode("utf-8")) <= 8192
            print(json.dumps({"checkpoints": checkpoints, "original_facts_preserved": facts, "recovery_bytes": len(history.encode('utf-8'))}), flush=True)
        finally:
            await backend.close()


if __name__ == "__main__":
    asyncio.run(main())
