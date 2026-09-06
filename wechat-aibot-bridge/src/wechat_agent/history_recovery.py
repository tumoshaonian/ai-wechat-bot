"""Bounded asynchronous channel-history compaction with atomic checkpoints."""

from collections.abc import Awaitable, Callable

from .conversation_journal import ConversationJournal
from .domain import AgentTaskInterrupted, UserVisibleError


async def recover_context(
    journal: ConversationJournal, chat: str, epoch: int, after: int, max_bytes: int,
    summarize: Callable[[str], Awaitable[str]],
    progress: Callable[[str], None],
) -> tuple[str, int]:
    for batch_number in range(9):
        if journal.epoch(chat) != epoch:
            raise AgentTaskInterrupted("会话已结束，停止历史恢复。")
        try:
            return journal.context(chat, epoch, after, max_bytes)
        except UserVisibleError as exc:
            if exc.code != "HISTORY_RECOVERY_BUDGET":
                raise
        if batch_number == 8:
            break
        batch = journal.summary_batch(chat, epoch, max_bytes)
        if batch is None:
            break
        previous, through, prompt = batch
        progress(f"正在整理历史记录，第 {batch_number + 1} 批")
        try:
            summary = (await summarize(prompt)).strip()
        except (AgentTaskInterrupted, UserVisibleError):
            raise
        except Exception as exc:
            raise UserVisibleError("历史摘要生成失败，原始记录未删除，本次操作未执行。", code="HISTORY_SUMMARY_FAILED") from exc
        if not summary or len(summary.encode("utf-8")) > max_bytes // 4:
            raise UserVisibleError("历史摘要为空或超出预算，未替换原始历史，本次操作未执行。", code="HISTORY_SUMMARY_INVALID")
        if not journal.save_checkpoint(chat, epoch, previous, through, summary):
            raise AgentTaskInterrupted("会话或恢复检查点已更新，请重新发起请求。")
        # Checkpoint may also cover pending channel facts in a live session.
        after = 0
    raise UserVisibleError("剩余历史包含过大的完整轮次或整理次数已达上限，原始记录保留，本次未操作电脑。", code="HISTORY_RECOVERY_BUDGET")
