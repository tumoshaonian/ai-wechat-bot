"""Opt-in real WinForms restoration test; never operates on user applications."""
import os
import subprocess
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from wechat_agent.desktop.worker import DesktopWorker


@unittest.skipUnless(os.name == "nt" and os.getenv("WECOM_RUN_DESKTOP_SMOKE") == "1", "requires an interactive Windows test desktop")
class WindowRestoreSmoke(unittest.TestCase):
    def test_minimized_and_offscreen_windows_three_times(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "windows_uia.ps1"
        with TemporaryDirectory() as root:
            worker = DesktopWorker(powershell_bin="powershell.exe", script_path=script, screenshot_directory=Path(root))
            for state in ("Minimized", "Normal"):
                for attempt in range(3):
                    title = "WeComRestoreTest-" + uuid4().hex
                    command = (
                        "Add-Type -AssemblyName System.Windows.Forms; "
                        "$f=New-Object System.Windows.Forms.Form; "
                        f"$f.Text='{title}'; "
                        "$f.Width=640; $f.Height=480; $f.StartPosition='Manual'; "
                        "$f.Location=New-Object System.Drawing.Point(-20000,-20000); "
                        f"$f.WindowState='{state}'; $f.ShowDialog() | Out-Null"
                    )
                    child = subprocess.Popen(["powershell.exe", "-NoProfile", "-Command", command], creationflags=subprocess.CREATE_NO_WINDOW)
                    try:
                        deadline = time.monotonic() + 10
                        while time.monotonic() < deadline:
                            windows = worker.list_windows({"process_name": "powershell", "title_contains": title})
                            if windows["count"]:
                                break
                            time.sleep(0.2)
                        result = worker.prepare_window({"process_name": "powershell", "title_contains": title})
                        self.assertTrue(result["ok"])
                        self.assertEqual(child.pid, result["window"]["process_id"])
                        self.assertFalse(result["window"]["offscreen"])
                        print(f"verified {state} iteration={attempt + 1} pid={child.pid}")
                    finally:
                        child.terminate()
                        child.wait(timeout=5)
