import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from wechat_agent.adapters.deepseek_harness import (
    DeepSeekHarnessBackend,
    _extract_file_deliveries,
    _friendly_harness_error,
    _harness_error_detail,
)
from wechat_agent.domain import AgentReply, IncomingMessage, UserVisibleError


class FakeHarness:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def run(self, input: str, *, session_id: str) -> SimpleNamespace:
        self.calls.append((input, session_id))
        return SimpleNamespace(final_response="任务完成", finish_reason="completed")

    def close(self) -> None:
        self.closed = True


class DeepSeekHarnessBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_stable_session_and_returns_final_response(self) -> None:
        with TemporaryDirectory() as temporary:
            harness = FakeHarness()
            backend = DeepSeekHarnessBackend(
                SimpleNamespace(harness_session_root=Path(temporary)),
                harness_factory=lambda _settings: harness,
            )
            incoming = IncomingMessage("m-1", "owner", "owner", "single", "打开记事本")

            first = await backend.reply(incoming)
            second = await backend.reply(incoming)
            await backend.close()

            self.assertEqual(first, AgentReply("任务完成"))
            self.assertEqual(second, AgentReply("任务完成"))
            self.assertEqual(harness.calls[0][1], harness.calls[1][1])
            self.assertRegex(harness.calls[0][1], r"^wecom-[0-9a-f]{24}-g0001$")
            self.assertTrue(harness.closed)

    async def test_end_rotates_without_deleting_previous_session(self) -> None:
        with TemporaryDirectory() as temporary:
            harness = FakeHarness()
            backend = DeepSeekHarnessBackend(
                SimpleNamespace(harness_session_root=Path(temporary)),
                harness_factory=lambda _settings: harness,
            )
            incoming = IncomingMessage("m-1", "owner", "owner", "single", "hello")

            await backend.reply(incoming)
            interrupted, status = await backend.end_session(incoming.session_id)
            await backend.reply(incoming)
            await backend.close()

            self.assertFalse(interrupted)
            self.assertEqual(status.generation, 2)
            self.assertTrue(harness.calls[0][1].endswith("-g0001"))
            self.assertTrue(harness.calls[1][1].endswith("-g0002"))

    def test_extracts_structured_harness_error(self) -> None:
        result = SimpleNamespace(
            events=[
                {
                    "type": "turn/end",
                    "data": {
                        "reason": {
                            "kind": "error",
                            "error": {"message": "restore failed", "code": "SESSION"},
                        }
                    },
                }
            ]
        )

        self.assertEqual(("restore failed", "SESSION"), _harness_error_detail(result))

    async def test_error_result_is_user_visible_and_rotates_session(self) -> None:
        class ErrorHarness(FakeHarness):
            def run(self, input: str, *, session_id: str) -> SimpleNamespace:
                self.calls.append((input, session_id))
                return SimpleNamespace(
                    final_response="",
                    finish_reason="error",
                    events=[
                        {
                            "type": "turn/end",
                            "data": {
                                "reason": {
                                    "error": {
                                        "message": "Insufficient Balance",
                                        "code": "QUOTA",
                                    }
                                }
                            },
                        }
                    ],
                )

        with TemporaryDirectory() as temporary:
            harness = ErrorHarness()
            backend = DeepSeekHarnessBackend(
                SimpleNamespace(harness_session_root=Path(temporary)),
                harness_factory=lambda _settings: harness,
            )
            incoming = IncomingMessage("m-1", "owner", "owner", "single", "hello")

            with self.assertRaises(UserVisibleError) as caught:
                await backend.reply(incoming)

            self.assertEqual("QUOTA", caught.exception.code)
            self.assertEqual(2, backend.session_status(incoming.session_id).generation)
            await backend.close()

    def test_extracts_and_validates_file_handoff_tags(self) -> None:
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "report with spaces.docx"
            path.write_text("content", encoding="utf-8")

            reply = _extract_file_deliveries(
                f"文件准备好了。\n<wechat-file>{path}</wechat-file>"
            )

            self.assertEqual("文件准备好了。", reply.text)
            self.assertEqual((path.resolve(),), reply.files)

    def test_quota_error_has_actionable_explanation(self) -> None:
        self.assertIn("余额不足", _friendly_harness_error("Insufficient Balance", "QUOTA"))


if __name__ == "__main__":
    unittest.main()
