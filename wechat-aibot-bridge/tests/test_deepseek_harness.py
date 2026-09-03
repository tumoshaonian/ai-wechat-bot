import asyncio
import json
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from wechat_agent.adapters.deepseek_harness import (
    DeepSeekHarnessBackend,
    _extract_desktop_tool_deliveries,
    _extract_file_deliveries,
    _friendly_harness_error,
    _friendly_harness_exception,
    _harness_error_detail,
    _create_harness,
    _normalize_error_code,
    _redact_harness_detail,
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
    def test_create_harness_uses_current_profile_sdk_contract(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            patch_file = root / "wechat.patch.yml"
            patch_file.write_text("[]\n", encoding="utf-8")
            settings = SimpleNamespace(
                harness_session_root=root / "registry",
                harness_dsh_home=root / "home",
                harness_system_prompt="WeCom persona",
                harness_permission_mode="danger-full-access",
                harness_runtime_mode="node",
                desktop_tools_enabled=False,
                desktop_action_timeout_seconds=180.0,
                task_timeout_seconds=480.0,
                harness_provider="deepseek-official",
                harness_model="deepseek-v4-flash",
                harness_reasoning_effort="max",
                harness_max_tokens=49152,
                harness_workspace=root,
                harness_dsh_bin=None,
                harness_profile="sdk",
                harness_patch_files=(patch_file,),
                harness_initialize_timeout_seconds=90.0,
                harness_request_timeout_seconds=480.0,
                harness_shutdown_timeout_seconds=10.0,
            )

            previous_mode = os.environ.get("DSH_RUNTIME_MODE")
            try:
                harness = _create_harness(settings)
                config = harness.config
                self.assertEqual("sdk", config.profile)
                self.assertEqual((str(patch_file),), config.patches)
                self.assertEqual(str(root / "home"), config.dsh_home)
                self.assertEqual("max", config.reasoning_effort)
                self.assertEqual(90.0, config.initialize_timeout_seconds)
                self.assertEqual(10.0, config.shutdown_timeout_seconds)
                self.assertEqual(str(root), config.runtime_cwd)
                self.assertEqual("danger-full-access", config.env["DSH_PERMISSION_MODE"])
                self.assertEqual("false", config.env["DSH_DESKTOP_ENABLED"])
                self.assertFalse(hasattr(config, "session_root"))
                self.assertFalse(hasattr(config, "cordis"))
                self.assertFalse(hasattr(config, "launch_args_override"))
                harness.close()
            finally:
                if previous_mode is None:
                    os.environ.pop("DSH_RUNTIME_MODE", None)
                else:
                    os.environ["DSH_RUNTIME_MODE"] = previous_mode

    async def test_start_initializes_runtime_and_closes_after_failure(self) -> None:
        class BrokenStartHarness(FakeHarness):
            def start(self) -> None:
                raise RuntimeError("profile failed")

        with TemporaryDirectory() as temporary:
            harness = BrokenStartHarness()
            backend = DeepSeekHarnessBackend(
                SimpleNamespace(harness_session_root=Path(temporary)),
                harness_factory=lambda _settings: harness,
            )

            with self.assertRaisesRegex(RuntimeError, "profile failed"):
                await backend.start()
            self.assertTrue(harness.closed)
            await backend.close()

    async def test_runtime_exception_is_closed_and_next_turn_gets_fresh_runtime(self) -> None:
        class BrokenRunHarness(FakeHarness):
            def run(self, input: str, *, session_id: str) -> SimpleNamespace:
                self.calls.append((input, session_id))
                raise TimeoutError("runtime stopped responding")

        with TemporaryDirectory() as temporary:
            first = BrokenRunHarness()
            second = FakeHarness()
            created = iter((first, second))
            backend = DeepSeekHarnessBackend(
                SimpleNamespace(harness_session_root=Path(temporary)),
                harness_factory=lambda _settings: next(created),
            )
            incoming = IncomingMessage("m-1", "owner", "owner", "single", "hello")

            with self.assertRaises(UserVisibleError) as caught:
                await backend.reply(incoming)
            reply = await backend.reply(incoming)
            await backend.close()

            self.assertEqual("HARNESS_REQUEST_TIMEOUT", caught.exception.code)
            self.assertIn("等待超时", str(caught.exception))
            self.assertTrue(first.closed)
            self.assertEqual(AgentReply("任务完成"), reply)
            self.assertTrue(first.calls[0][1].endswith("-g0001"))
            self.assertTrue(second.calls[0][1].endswith("-g0002"))

    async def test_cancelled_reply_closes_runtime_and_rotates_session(self) -> None:
        class BlockingHarness(FakeHarness):
            def __init__(self) -> None:
                super().__init__()
                self.started = threading.Event()
                self.released = threading.Event()

            def run(self, input: str, *, session_id: str) -> SimpleNamespace:
                self.calls.append((input, session_id))
                self.started.set()
                self.released.wait(timeout=3)
                return SimpleNamespace(
                    final_response="should not be delivered",
                    finish_reason="completed",
                    events=[],
                )

            def close(self) -> None:
                super().close()
                self.released.set()

        with TemporaryDirectory() as temporary:
            harness = BlockingHarness()
            backend = DeepSeekHarnessBackend(
                SimpleNamespace(harness_session_root=Path(temporary)),
                harness_factory=lambda _settings: harness,
            )
            incoming = IncomingMessage("m-cancel", "owner", "owner", "single", "wait")
            task = asyncio.create_task(backend.reply(incoming))
            self.assertTrue(await asyncio.to_thread(harness.started.wait, 1))

            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            self.assertTrue(harness.closed)
            self.assertEqual(2, backend.session_status(incoming.session_id).generation)
            await backend.close()

    async def test_non_tool_notification_does_not_crash_the_agent(self) -> None:
        class NotificationHarness(FakeHarness):
            def run(
                self,
                input: str,
                *,
                session_id: str,
                on_notification,
            ) -> SimpleNamespace:
                self.calls.append((input, session_id))
                on_notification(
                    SimpleNamespace(
                        method="session.status",
                        payload={"status": "running"},
                    )
                )
                on_notification(
                    SimpleNamespace(
                        method="subagent.started",
                        payload={"parentSessionId": session_id, "childSessionId": "child"},
                    )
                )
                on_notification(
                    SimpleNamespace(
                        method="subagent.finished",
                        payload={"parentSessionId": session_id, "childSessionId": "child"},
                    )
                )
                return SimpleNamespace(
                    final_response="你好！",
                    finish_reason="completed",
                )

        with TemporaryDirectory() as temporary:
            harness = NotificationHarness()
            backend = DeepSeekHarnessBackend(
                SimpleNamespace(harness_session_root=Path(temporary)),
                harness_factory=lambda _settings: harness,
            )

            reply = await backend.reply(
                IncomingMessage("m-notification", "owner", "owner", "single", "你好")
            )
            await backend.close()

            self.assertEqual(AgentReply("你好！"), reply)

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
            self.assertRegex(
                harness.calls[0][1],
                r"^wecom-[0-9a-f]{24}-g0001$",
            )
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

    async def test_max_tokens_is_returned_as_explicit_partial_result(self) -> None:
        class MaxTokensHarness(FakeHarness):
            def run(self, input: str, *, session_id: str) -> SimpleNamespace:
                self.calls.append((input, session_id))
                return SimpleNamespace(
                    final_response="已完成前半部分",
                    finish_reason="max-tokens",
                    events=[],
                )

        with TemporaryDirectory() as temporary:
            harness = MaxTokensHarness()
            backend = DeepSeekHarnessBackend(
                SimpleNamespace(harness_session_root=Path(temporary)),
                harness_factory=lambda _settings: harness,
            )

            reply = await backend.reply(
                IncomingMessage("m-1", "owner", "owner", "single", "long task")
            )
            await backend.close()

            self.assertIn("已完成前半部分", reply.text)
            self.assertIn("可能不完整", reply.text)

    async def test_missing_finish_reason_rebuilds_runtime(self) -> None:
        class InvalidResultHarness(FakeHarness):
            def run(self, input: str, *, session_id: str) -> SimpleNamespace:
                self.calls.append((input, session_id))
                return SimpleNamespace(
                    final_response="看似完成",
                    finish_reason=None,
                    events=[],
                )

        with TemporaryDirectory() as temporary:
            harness = InvalidResultHarness()
            backend = DeepSeekHarnessBackend(
                SimpleNamespace(harness_session_root=Path(temporary)),
                harness_factory=lambda _settings: harness,
            )
            incoming = IncomingMessage("m-1", "owner", "owner", "single", "hello")

            with self.assertRaises(UserVisibleError) as caught:
                await backend.reply(incoming)
            await backend.close()

            self.assertEqual("AGENT_PROTOCOL_ERROR", caught.exception.code)
            self.assertTrue(harness.closed)
            self.assertEqual(2, backend.session_status(incoming.session_id).generation)

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

    def test_stable_harness_errors_are_actionable_and_never_echo_detail(self) -> None:
        secret_detail = (
            "api_key=sk-test-secret Bearer abcdefgh "
            r"C:\private\provider-diagnostic.txt"
        )
        expectations = {
            "MISSING_CREDENTIAL": "未配置",
            "INVALID_CREDENTIAL": "格式无效",
            "NO_ADAPTER": "模型提供商",
            "UNKNOWN_MODEL": "不支持配置的模型",
            "CONTEXT_WINDOW_EXCEEDED": "上下文上限",
            "TIMEOUT": "响应超时",
            "TRANSPORT": "网络连接中断",
            "SERVER": "服务暂时异常",
            "EMPTY_RESPONSE": "没有返回有效内容",
            "INVALID_REQUEST": "拒绝了本次请求",
            "TOOL_TIMEOUT": "电脑操作工具执行超时",
        }

        for code, expected in expectations.items():
            with self.subTest(code=code):
                message = _friendly_harness_error(secret_detail, code)
                self.assertIn(expected, message)
                self.assertNotIn("sk-test-secret", message)
                self.assertNotIn("abcdefgh", message)
                self.assertNotIn(r"C:\private", message)

    def test_unknown_harness_error_does_not_echo_detail_or_unsafe_code(self) -> None:
        detail = r"api_key=sk-secret C:\private\failure.log"
        message = _friendly_harness_error(detail, "BAD CODE: secret")

        self.assertIn("UNKNOWN", message)
        self.assertNotIn("BAD CODE", message)
        self.assertNotIn("sk-secret", message)
        self.assertNotIn(r"C:\private", message)
        self.assertEqual("UNKNOWN", _normalize_error_code("x" * 65))

    def test_sdk_exception_mapping_keeps_protocol_and_domain_errors_separate(self) -> None:
        from deepseek_harness.errors import (
            JsonRpcError,
            SdkProtocolError,
            TransportClosedError,
        )

        cases = (
            (TimeoutError("slow"), "HARNESS_REQUEST_TIMEOUT"),
            (TransportClosedError("closed"), "HARNESS_TRANSPORT_CLOSED"),
            (SdkProtocolError("bad frame"), "HARNESS_PROTOCOL_ERROR"),
            (JsonRpcError(-32603, "profile failed"), "HARNESS_RPC_ERROR"),
            (FileNotFoundError("missing runtime"), "HARNESS_RUNTIME_UNAVAILABLE"),
            (RuntimeError("unknown"), "HARNESS_RUNTIME_ERROR"),
        )

        for error, expected_code in cases:
            with self.subTest(error=type(error).__name__):
                code, message = _friendly_harness_exception(error, phase="request")
                self.assertEqual(expected_code, code)
                self.assertTrue(message)
                self.assertNotIn(str(error), message)

    def test_harness_diagnostics_redact_configured_and_inline_credentials(self) -> None:
        previous = os.environ.get("DEEPSEEK_API_KEY")
        os.environ["DEEPSEEK_API_KEY"] = "sk-configured-secret"
        try:
            redacted = _redact_harness_detail(
                "sk-configured-secret api_key=sk-inline-secret Bearer abc.def"
            )
        finally:
            if previous is None:
                os.environ.pop("DEEPSEEK_API_KEY", None)
            else:
                os.environ["DEEPSEEK_API_KEY"] = previous

        self.assertNotIn("sk-configured-secret", redacted)
        self.assertNotIn("sk-inline-secret", redacted)
        self.assertNotIn("abc.def", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_session_collision_has_clear_chinese_explanation(self) -> None:
        detail = (
            'session "wecom-example" already has a persisted log on disk '
            "that does not match this live session (id collision)"
        )
        result = SimpleNamespace(
            events=[
                {
                    "type": "turn/end",
                    "data": {
                        "reason": {
                            "error": {"message": detail, "code": "UNKNOWN"}
                        }
                    },
                }
            ]
        )

        self.assertEqual((detail, "SESSION_COLLISION"), _harness_error_detail(result))
        explanation = _friendly_harness_error(detail, "SESSION_COLLISION")
        self.assertIn("旧会话记录", explanation)
        self.assertIn("尚未执行电脑操作", explanation)
        self.assertIn("重新发送", explanation)

    def test_recovers_screenshot_from_successful_desktop_tool_event(self) -> None:
        with TemporaryDirectory() as temporary:
            screenshot = Path(temporary) / "doubao.png"
            screenshot.write_bytes(b"png")
            events = _desktop_tool_events(
                "mcp__desktop__doubao_ask",
                {"ok": True, "submitted": True, "screenshot_path": str(screenshot)},
            )

            self.assertEqual(
                (screenshot.resolve(),),
                _extract_desktop_tool_deliveries(events),
            )

    def test_ignores_paths_from_untrusted_or_failed_tool_events(self) -> None:
        with TemporaryDirectory() as temporary:
            screenshot = Path(temporary) / "not-a-delivery.png"
            screenshot.write_bytes(b"png")
            shell_events = _desktop_tool_events(
                "bash",
                {"screenshot_path": str(screenshot)},
            )
            failed_events = _desktop_tool_events(
                "mcp__desktop__capture",
                {"screenshot_path": str(screenshot)},
                is_error=True,
            )

            self.assertEqual((), _extract_desktop_tool_deliveries(shell_events))
            self.assertEqual((), _extract_desktop_tool_deliveries(failed_events))

    def test_recovers_descendant_screenshots_without_replaying_root_events(self) -> None:
        with TemporaryDirectory() as temporary:
            root_screenshot = Path(temporary) / "root.png"
            child_screenshot = Path(temporary) / "child.png"
            unrelated_screenshot = Path(temporary) / "unrelated.png"
            for screenshot in (root_screenshot, child_screenshot, unrelated_screenshot):
                screenshot.write_bytes(b"png")

            root_events = _desktop_tool_events(
                "mcp__desktop__capture",
                {"screenshot_path": str(root_screenshot)},
                call_id="same-call-id",
            )
            child_events = _desktop_tool_events(
                "mcp__desktop__capture",
                {"screenshot_path": str(child_screenshot)},
                call_id="same-call-id",
            )
            # A different descendant may legally reuse a call id. Its result
            # must not be paired with the trusted call from the first child.
            unrelated_result = _desktop_tool_events(
                "bash",
                {"screenshot_path": str(unrelated_screenshot)},
                call_id="same-call-id",
            )[1:]
            notifications = [
                # The SDK repeats root events in notifications. They must not
                # duplicate deliveries already recovered from result.events.
                *_session_event_notifications("root", root_events),
                *_session_event_notifications("child-a", child_events),
                *_session_event_notifications("child-b", unrelated_result),
            ]

            self.assertEqual(
                (root_screenshot.resolve(), child_screenshot.resolve()),
                _extract_desktop_tool_deliveries(
                    root_events,
                    notifications=notifications,
                    root_session_id="root",
                ),
            )

    async def test_backend_merges_tool_screenshot_without_model_file_tag(self) -> None:
        with TemporaryDirectory() as temporary:
            screenshot = Path(temporary) / "doubao.png"
            screenshot.write_bytes(b"png")

            class DesktopHarness(FakeHarness):
                def run(self, input: str, *, session_id: str) -> SimpleNamespace:
                    self.calls.append((input, session_id))
                    return SimpleNamespace(
                        final_response="豆包已回答，截图准备完成。",
                        finish_reason="completed",
                        events=_desktop_tool_events(
                            "mcp__desktop__doubao_ask",
                            {"screenshot_path": str(screenshot)},
                        ),
                    )

            harness = DesktopHarness()
            backend = DeepSeekHarnessBackend(
                SimpleNamespace(harness_session_root=Path(temporary) / "sessions"),
                harness_factory=lambda _settings: harness,
            )

            reply = await backend.reply(
                IncomingMessage("m-1", "owner", "owner", "single", "打开豆包并截图")
            )
            await backend.close()

            self.assertEqual("豆包已回答，截图准备完成。", reply.text)
            self.assertEqual((screenshot.resolve(),), reply.files)

    async def test_backend_merges_descendant_tool_screenshot_from_notifications(self) -> None:
        with TemporaryDirectory() as temporary:
            screenshot = Path(temporary) / "subagent.png"
            screenshot.write_bytes(b"png")

            class SubagentHarness(FakeHarness):
                def run(self, input: str, *, session_id: str) -> SimpleNamespace:
                    self.calls.append((input, session_id))
                    descendant_events = _desktop_tool_events(
                        "mcp__desktop__doubao_ask",
                        {"ok": True, "screenshot_path": str(screenshot)},
                    )
                    return SimpleNamespace(
                        session_id=session_id,
                        final_response="子 Agent 已完成豆包任务。",
                        finish_reason="completed",
                        events=[],
                        notifications=_session_event_notifications(
                            "child-session",
                            descendant_events,
                        ),
                    )

            harness = SubagentHarness()
            backend = DeepSeekHarnessBackend(
                SimpleNamespace(harness_session_root=Path(temporary) / "sessions"),
                harness_factory=lambda _settings: harness,
            )

            reply = await backend.reply(
                IncomingMessage("m-child", "owner", "owner", "single", "让子 Agent 截图")
            )
            await backend.close()

            self.assertEqual("子 Agent 已完成豆包任务。", reply.text)
            self.assertEqual((screenshot.resolve(),), reply.files)


def _desktop_tool_events(
    tool_name: str,
    result: dict[str, object],
    *,
    is_error: bool = False,
    call_id: str = "desktop-call-1",
) -> list[dict[str, object]]:
    return [
        {
            "type": "tool/call",
            "data": {"callId": call_id, "name": tool_name},
        },
        {
            "type": "tool/result",
            "data": {
                "message": {
                    "source": {"callId": call_id},
                    "content": [
                        {
                            "isError": is_error,
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result),
                                }
                            ],
                        }
                    ],
                }
            },
        },
    ]


def _session_event_notifications(
    session_id: str,
    events: list[dict[str, object]],
) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            method="session.event",
            payload={"sessionId": session_id, "event": event},
        )
        for event in events
    ]


if __name__ == "__main__":
    unittest.main()
