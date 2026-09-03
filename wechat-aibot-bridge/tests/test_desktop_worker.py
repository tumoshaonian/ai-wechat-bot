import base64
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from wechat_agent.desktop.mcp_server import TOOLS, handle_request
from wechat_agent.desktop.worker import DesktopWorker, DesktopWorkerError


class RecordingRunner:
    def __init__(self, result: dict | None = None) -> None:
        self.result = result or {"ok": True, "count": 0, "windows": []}
        self.commands: list[list[str]] = []
        self.options: list[dict] = []

    def __call__(self, command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        self.options.append(kwargs)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(self.result, ensure_ascii=False),
            stderr="",
        )


class FakeWorker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _call(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        return {"ok": True, "tool": name}

    def list_windows(self, arguments):
        return self._call("list_windows", arguments)

    def inspect_window(self, arguments):
        return self._call("inspect_window", arguments)

    def set_value(self, arguments):
        return self._call("set_value", arguments)

    def invoke(self, arguments):
        return self._call("invoke", arguments)

    def capture(self, arguments):
        return self._call("capture", arguments)

    def ask_doubao(self, arguments):
        return self._call("doubao_ask", arguments)


class DesktopWorkerTests(unittest.TestCase):
    def test_encodes_payload_without_shell_interpolation(self) -> None:
        with TemporaryDirectory() as temporary:
            runner = RecordingRunner()
            worker = DesktopWorker(
                powershell_bin="powershell.exe",
                script_path=Path(temporary) / "windows_uia.ps1",
                screenshot_directory=Path(temporary),
                runner=runner,
            )

            result = worker.list_windows({"title_contains": "豆包'$(unsafe)"})

            self.assertTrue(result["ok"])
            command = runner.commands[0]
            payload = command[command.index("-PayloadBase64") + 1]
            decoded = json.loads(base64.b64decode(payload).decode("utf-8"))
            self.assertEqual("豆包'$(unsafe)", decoded["title_contains"])
            self.assertNotIn("豆包'$(unsafe)", command)

    def test_doubao_action_adds_reviewed_defaults(self) -> None:
        with TemporaryDirectory() as temporary:
            runner = RecordingRunner({"ok": True, "submitted": True})
            worker = DesktopWorker(
                powershell_bin="powershell.exe",
                script_path=Path(temporary) / "windows_uia.ps1",
                screenshot_directory=Path(temporary) / "shots",
                doubao_launch_path=Path(temporary) / "豆包.lnk",
                runner=runner,
            )

            worker.ask_doubao({"question": "什么是计算机网络"})

            command = runner.commands[0]
            payload = command[command.index("-PayloadBase64") + 1]
            decoded = json.loads(base64.b64decode(payload).decode("utf-8"))
            self.assertEqual("Doubao", decoded["process_name"])
            self.assertEqual(120, decoded["answer_timeout_seconds"])
            self.assertTrue(decoded["launch_path"].endswith("豆包.lnk"))
            self.assertEqual(180, runner.options[0]["timeout"])

    def test_rejects_native_failure_result(self) -> None:
        with TemporaryDirectory() as temporary:
            worker = DesktopWorker(
                powershell_bin="powershell.exe",
                script_path=Path(temporary) / "windows_uia.ps1",
                screenshot_directory=Path(temporary),
                runner=RecordingRunner({"ok": False, "error": "window missing"}),
            )

            with self.assertRaisesRegex(DesktopWorkerError, "window missing"):
                worker.list_windows({})

    def test_native_failure_includes_exact_stage(self) -> None:
        with TemporaryDirectory() as temporary:
            worker = DesktopWorker(
                powershell_bin="powershell.exe",
                script_path=Path(temporary) / "windows_uia.ps1",
                screenshot_directory=Path(temporary),
                runner=RecordingRunner({
                    "ok": False,
                    "stage": "verify-question-submitted",
                    "error": "question was not visible",
                }),
            )

            with self.assertRaisesRegex(
                DesktopWorkerError,
                r"\[stage=verify-question-submitted\] question was not visible",
            ):
                worker.ask_doubao({"question": "测试"})

    def test_generic_window_listing_uses_short_timeout(self) -> None:
        with TemporaryDirectory() as temporary:
            runner = RecordingRunner()
            worker = DesktopWorker(
                powershell_bin="powershell.exe",
                script_path=Path(temporary) / "windows_uia.ps1",
                timeout_seconds=180,
                screenshot_directory=Path(temporary),
                runner=runner,
            )

            worker.list_windows({})

            self.assertEqual(12, runner.options[0]["timeout"])
            self.assertEqual(subprocess.DEVNULL, runner.options[0]["stdin"])

    def test_preflight_uses_its_independent_short_timeout(self) -> None:
        with TemporaryDirectory() as temporary:
            runner = RecordingRunner()
            worker = DesktopWorker(
                powershell_bin="powershell.exe",
                script_path=Path(temporary) / "windows_uia.ps1",
                timeout_seconds=300,
                preflight_timeout_seconds=25,
                screenshot_directory=Path(temporary),
                runner=runner,
            )

            worker.preflight()

            self.assertEqual(25, runner.options[0]["timeout"])

    def test_doubao_timeout_arguments_cannot_exceed_external_contract(self) -> None:
        with TemporaryDirectory() as temporary:
            worker = DesktopWorker(
                powershell_bin="powershell.exe",
                script_path=Path(temporary) / "windows_uia.ps1",
                screenshot_directory=Path(temporary),
                runner=RecordingRunner(),
            )

            with self.assertRaisesRegex(
                DesktopWorkerError,
                "answer_timeout_seconds must be between 5 and 120 seconds",
            ):
                worker.ask_doubao({"question": "测试", "answer_timeout_seconds": 121})

    def test_action_timeout_must_cover_advertised_doubao_budget(self) -> None:
        with TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                ValueError,
                "timeout_seconds must be at least 180s",
            ):
                DesktopWorker(
                    powershell_bin="powershell.exe",
                    script_path=Path(temporary) / "windows_uia.ps1",
                    timeout_seconds=179,
                    screenshot_directory=Path(temporary),
                )

    def test_mcp_lists_doubao_and_structured_uia_tools(self) -> None:
        response = handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            FakeWorker(),  # type: ignore[arg-type]
        )

        names = {tool["name"] for tool in response["result"]["tools"]}  # type: ignore[index]
        self.assertEqual({
            "list_windows",
            "inspect_window",
            "set_value",
            "invoke",
            "capture",
            "doubao_ask",
        }, names)
        self.assertEqual(names, {tool["name"] for tool in TOOLS})
        doubao_schema = next(
            tool["inputSchema"]
            for tool in TOOLS
            if tool["name"] == "doubao_ask"
        )
        properties = doubao_schema["properties"]
        self.assertEqual(30, properties["window_timeout_seconds"]["maximum"])
        self.assertEqual(120, properties["answer_timeout_seconds"]["maximum"])

    def test_mcp_dispatches_doubao_action_and_returns_structured_content(self) -> None:
        worker = FakeWorker()
        response = handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "doubao_ask",
                    "arguments": {"question": "你好"},
                },
            },
            worker,  # type: ignore[arg-type]
        )

        self.assertEqual([("doubao_ask", {"question": "你好"})], worker.calls)
        result = response["result"]  # type: ignore[index]
        self.assertEqual({"ok": True, "tool": "doubao_ask"}, result["structuredContent"])
        self.assertFalse(result.get("isError", False))

    def test_harness_profile_patch_requires_desktop_tool_discovery(self) -> None:
        config = (
            Path(__file__).parents[1]
            / "config"
            / "harness-wecom.patch.yml"
        ).read_text(encoding="utf-8")

        self.assertIn("- insert:", config)
        self.assertIn("- id: desktop-mcp", config)
        self.assertIn("name: '@deepseek-ai/dsh-mcp-client'", config)
        self.assertIn("failOnStartupError: true", config)
        self.assertIn("maxAttempts: 10", config)
        self.assertIn("backgroundMode: one-shot", config)
        self.assertNotIn("sdk-jsonrpc-server", config)
        self.assertFalse(
            (Path(__file__).parents[1] / "scripts" / "sdk_after_desktop.mjs").exists()
        )

        uia_script = (
            Path(__file__).parents[1]
            / "scripts"
            / "windows_uia.ps1"
        ).read_text(encoding="utf-8")
        self.assertIn("NativeTopLevelWindowEnumerator", uia_script)
        self.assertIn("DesktopWorkerWatchdog", uia_script)
        self.assertIn("--force-renderer-accessibility", uia_script)
        self.assertIn("PrintWindow", uia_script)
        self.assertIn("accessibility_restarted", uia_script)
        self.assertIn("Sort-Object { $_.Current.BoundingRectangle.Width", uia_script)
        self.assertIn("controls = @($controls | ForEach-Object { $_ })", uia_script)

        server = (
            Path(__file__).parents[1]
            / "src"
            / "wechat_agent"
            / "desktop"
            / "mcp_server.py"
        ).read_text(encoding="utf-8")
        self.assertIn("preflight = worker.preflight()", server)
        self.assertIn("preflight_windows=%s", server)


if __name__ == "__main__":
    unittest.main()
