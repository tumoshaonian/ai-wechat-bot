from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import logging
import os
import re
import subprocess
import time
import winreg
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import psutil
import pyautogui
import pyperclip
import requests
import win32con
import win32gui
import win32process
from ctypes import wintypes
from PIL import ImageGrab
from rapidocr_onnxruntime import RapidOCR


TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
MENTION_TOKEN_RE = re.compile(r"[@＠]\s*([^\s@:：,，。!?！？]+)")
ABSOLUTE_PATH_RE = re.compile(r'([a-zA-Z]:\\[^<>:"|?*\r\n]+)')
FILE_NAME_RE = re.compile(r'([A-Za-z0-9_\-\u4e00-\u9fa5 .()（）]+?\.[A-Za-z0-9]{1,10})')
FILE_PREFIX_RE = re.compile(
    r"^(?:请|帮我|帮忙|麻烦|把|将|给我|发我|发给我|发送|电脑|桌面|文档|下载|上的|里的|中的|电脑的|桌面的|文档的|下载的)+",
    re.IGNORECASE,
)
UI_NOISE_TEXTS = {
    "微信",
    "消息",
    "会话",
    "搜索",
    "聊天信息",
    "设置",
    "通讯录",
    "小程序面板",
    "搜 索",
}
DROPFILES_GHND = 0x0042
CF_HDROP = 15
SIZE_T = ctypes.c_size_t
LPVOID = ctypes.c_void_p
HGLOBAL = ctypes.c_void_p

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, SIZE_T]
kernel32.GlobalAlloc.restype = HGLOBAL
kernel32.GlobalLock.argtypes = [HGLOBAL]
kernel32.GlobalLock.restype = LPVOID
kernel32.GlobalUnlock.argtypes = [HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = [HGLOBAL]
kernel32.GlobalFree.restype = HGLOBAL
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.SetClipboardData.argtypes = [wintypes.UINT, HGLOBAL]
user32.SetClipboardData.restype = HGLOBAL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL


class DROPFILES(ctypes.Structure):
    _fields_ = [
        ("pFiles", wintypes.DWORD),
        ("pt_x", wintypes.LONG),
        ("pt_y", wintypes.LONG),
        ("fNC", wintypes.BOOL),
        ("fWide", wintypes.BOOL),
    ]


@dataclass
class BridgeConfig:
    spring_boot_url: str = "http://127.0.0.1:8080/api/wechat/reply"
    listen_contacts: list[str] = field(default_factory=list)
    listen_groups: list[str] = field(default_factory=list)
    group_trigger_prefixes: list[str] = field(default_factory=list)
    poll_interval_seconds: float = 1.0
    request_timeout_seconds: float = 30.0
    reply_prefix: str = ""
    max_processed_messages: int = 500
    ignore_self_messages: bool = True
    weixin_path: str = ""
    maximize_main_window: bool = False
    chat_open_wait_seconds: float = 0.8
    search_result_timeout_seconds: float = 0.6
    send_delay_seconds: float = 0.2
    focus_window_wait_seconds: float = 0.8
    debug_save_images: bool = False

    @classmethod
    def load(cls, path: Path) -> "BridgeConfig":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(**data)


@dataclass
class IncomingMessage:
    chat_name: str
    sender: str
    content: str
    is_group_chat: bool
    is_self: bool
    fingerprint: str


@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    class_name: str
    title: str
    rect: tuple[int, int, int, int]

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])


@dataclass
class OcrBlock:
    text: str
    score: float
    left: float
    top: float
    right: float
    bottom: float

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2


@dataclass
class MessageCluster:
    side: str
    lines: list[OcrBlock]

    @property
    def bottom(self) -> float:
        return max(line.bottom for line in self.lines)

    @property
    def left(self) -> float:
        return min(line.left for line in self.lines)

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines if line.text)


class ProcessedMessageCache:
    def __init__(self, capacity: int) -> None:
        self.capacity = max(1, capacity)
        self._queue: deque[str] = deque()
        self._lookup: set[str] = set()

    def contains(self, fingerprint: str) -> bool:
        return fingerprint in self._lookup

    def add(self, fingerprint: str) -> None:
        if fingerprint in self._lookup:
            return
        self._queue.append(fingerprint)
        self._lookup.add(fingerprint)
        if len(self._queue) > self.capacity:
            removed = self._queue.popleft()
            self._lookup.discard(removed)


class SpringBootClient:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.session = requests.Session()

    def get_reply(self, message: IncomingMessage) -> str:
        payload = {
            "message": message.content,
            "sessionId": self.build_session_id(message),
            "fromUser": message.sender,
            "chatName": message.chat_name,
            "groupChat": message.is_group_chat,
            "source": "wechat",
        }
        response = self.session.post(
            self.config.spring_boot_url,
            json=payload,
            timeout=self.config.request_timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        reply = str(body.get("reply", "")).strip()
        if not reply:
            raise ValueError("Spring Boot returned an empty reply")
        return reply

    @staticmethod
    def build_session_id(message: IncomingMessage) -> str:
        prefix = "group" if message.is_group_chat else "friend"
        return f"wechat:{prefix}:{message.chat_name}"


class WeixinController:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.ocr = RapidOCR()
        self.debug_dir = Path(__file__).resolve().parent / "debug"
        pyautogui.FAILSAFE = False

    def ensure_ready(self) -> WindowInfo:
        window = self.find_main_window()
        if window is None:
            self.start_weixin()
            window = self.wait_for_main_window(15.0)
        self.activate_window(window)
        return window

    def read_latest_message(self, chat_name: str, is_group_chat: bool) -> IncomingMessage | None:
        window = self.ensure_ready()
        active_chat_name = self.read_active_chat_name(window)
        if not self.chat_title_matches(chat_name, active_chat_name):
            raise RuntimeError(f"当前会话不是目标对象: expected={chat_name}, actual={active_chat_name or '<empty>'}")
        return self.read_latest_message_from_current_chat(window, chat_name, is_group_chat)

    def read_latest_message_from_current_chat(
        self,
        window: WindowInfo,
        chat_name: str,
        is_group_chat: bool,
    ) -> IncomingMessage | None:
        image = self.capture_region(window, self.message_region(window), f"{chat_name}_messages")
        blocks = self.ocr_blocks(image)
        return self.parse_latest_message(chat_name, is_group_chat, window, image, blocks)

    def send_text(self, chat_name: str, text: str) -> None:
        if not text.strip():
            return
        window = self.ensure_ready()
        active_chat_name = self.read_active_chat_name(window)
        if not self.chat_title_matches(chat_name, active_chat_name):
            raise RuntimeError(f"发送前会话校验失败: expected={chat_name}, actual={active_chat_name or '<empty>'}")
        self.click_relative(window, 0.55, 0.92)
        pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(self.config.send_delay_seconds)
        pyautogui.hotkey("alt", "s")

    def send_file(self, chat_name: str, file_path: Path) -> None:
        window = self.ensure_ready()
        active_chat_name = self.read_active_chat_name(window)
        if not self.chat_title_matches(chat_name, active_chat_name):
            raise RuntimeError(f"发送文件前会话校验失败: expected={chat_name}, actual={active_chat_name or '<empty>'}")
        self.click_relative(window, 0.55, 0.92)
        copy_files_to_clipboard([file_path])
        pyautogui.hotkey("ctrl", "v")
        time.sleep(self.config.send_delay_seconds)
        pyautogui.hotkey("alt", "s")

    def open_chat(self, chat_name: str) -> WindowInfo:
        window = self.ensure_ready()
        attempts = 2
        for attempt in range(1, attempts + 1):
            self.click_relative(window, 0.17, 0.055)
            time.sleep(0.1)
            pyautogui.hotkey("ctrl", "a")
            pyautogui.press("backspace")
            pyperclip.copy(chat_name)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(self.config.search_result_timeout_seconds)
            search_results_text = self.read_search_results_text(window)
            if not self.chat_title_matches(chat_name, search_results_text):
                logging.warning(
                    "Search result verification failed for %s (attempt %s/%s), results: %s",
                    chat_name,
                    attempt,
                    attempts,
                    search_results_text or "<empty>",
                )
                pyautogui.press("esc")
                continue

            self.click_relative(window, 0.17, 0.16)
            time.sleep(self.config.chat_open_wait_seconds)

            current_title = self.read_current_chat_title(window)
            if not current_title or self.chat_title_matches(chat_name, current_title):
                return window

            logging.warning(
                "Open chat verification failed for %s (attempt %s/%s), current title: %s",
                chat_name,
                attempt,
                attempts,
                current_title or "<empty>",
            )

        raise RuntimeError(f"未成功切换到目标会话: {chat_name}")

    def find_main_window(self) -> WindowInfo | None:
        windows = self.discover_weixin_windows()
        if not windows:
            return None
        return max(windows, key=lambda item: item.width * item.height)

    def wait_for_main_window(self, timeout_seconds: float) -> WindowInfo:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            window = self.find_main_window()
            if window is not None:
                return window
            time.sleep(0.5)
        raise RuntimeError("未检测到新版微信主窗口，请先登录并打开微信 4.x。")

    def start_weixin(self) -> None:
        weixin_path = self.resolve_weixin_path()
        if not weixin_path:
            raise RuntimeError(
                "未找到 Weixin.exe 路径，请在 config.json 里设置 weixin_path 或配置环境变量 WEIXIN_PATH。"
            )
        subprocess.Popen([weixin_path])
        time.sleep(2.0)

    def resolve_weixin_path(self) -> str:
        if self.config.weixin_path:
            configured = Path(self.config.weixin_path).expanduser()
            if configured.exists():
                return str(configured)

        env_path = os.environ.get("WEIXIN_PATH", "").strip()
        if env_path and Path(env_path).exists():
            return env_path

        for process in psutil.process_iter(["name", "exe"]):
            name = str(process.info.get("name") or "").lower()
            exe_path = process.info.get("exe")
            if name == "weixin.exe" and exe_path:
                return str(exe_path)

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin") as key:
                install_dir = winreg.QueryValueEx(key, "InstallPath")[0]
            candidate = Path(install_dir) / "Weixin.exe"
            if candidate.exists():
                return str(candidate)
        except FileNotFoundError:
            return ""
        return ""

    def activate_window(self, window: WindowInfo) -> None:
        hwnd = window.hwnd
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetWindowPos(
            hwnd,
            win32con.HWND_TOPMOST,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
        )
        win32gui.SetWindowPos(
            hwnd,
            win32con.HWND_NOTOPMOST,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE,
        )
        if self.config.maximize_main_window:
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        time.sleep(self.config.focus_window_wait_seconds)

    def discover_weixin_windows(self) -> list[WindowInfo]:
        weixin_pids = {
            process.pid
            for process in psutil.process_iter(["name"])
            if (process.info.get("name") or "").lower() == "weixin.exe"
        }
        windows: list[WindowInfo] = []

        def callback(hwnd: int, container: list[WindowInfo]) -> None:
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid not in weixin_pids:
                    return
                rect = win32gui.GetWindowRect(hwnd)
                info = WindowInfo(
                    hwnd=hwnd,
                    pid=pid,
                    class_name=win32gui.GetClassName(hwnd),
                    title=win32gui.GetWindowText(hwnd),
                    rect=rect,
                )
                if info.width > 300 and info.height > 300:
                    container.append(info)
            except Exception:
                return

        win32gui.EnumWindows(callback, windows)
        return windows

    def click_relative(self, window: WindowInfo, x_ratio: float, y_ratio: float) -> None:
        x = int(window.rect[0] + window.width * x_ratio)
        y = int(window.rect[1] + window.height * y_ratio)
        pyautogui.click(x, y)

    def message_region(self, window: WindowInfo) -> tuple[int, int, int, int]:
        left = int(window.rect[0] + window.width * 0.33)
        top = int(window.rect[1] + window.height * 0.12)
        right = int(window.rect[0] + window.width * 0.96)
        bottom = int(window.rect[1] + window.height * 0.78)
        return left, top, right, bottom

    def title_region(self, window: WindowInfo) -> tuple[int, int, int, int]:
        left = int(window.rect[0] + window.width * 0.34)
        top = int(window.rect[1] + window.height * 0.02)
        right = int(window.rect[0] + window.width * 0.78)
        bottom = int(window.rect[1] + window.height * 0.11)
        return left, top, right, bottom

    def search_result_region(self, window: WindowInfo) -> tuple[int, int, int, int]:
        left = int(window.rect[0] + window.width * 0.03)
        top = int(window.rect[1] + window.height * 0.09)
        right = int(window.rect[0] + window.width * 0.31)
        bottom = int(window.rect[1] + window.height * 0.30)
        return left, top, right, bottom

    def session_list_region(self, window: WindowInfo) -> tuple[int, int, int, int]:
        left = int(window.rect[0] + window.width * 0.02)
        top = int(window.rect[1] + window.height * 0.10)
        right = int(window.rect[0] + window.width * 0.31)
        bottom = int(window.rect[1] + window.height * 0.78)
        return left, top, right, bottom

    def capture_region(
        self,
        window: WindowInfo,
        region: tuple[int, int, int, int],
        debug_name: str,
    ) -> Any:
        self.activate_window(window)
        image = ImageGrab.grab(region)
        if self.config.debug_save_images:
            self.debug_dir.mkdir(exist_ok=True)
            image.save(self.debug_dir / f"{safe_filename(debug_name)}.png")
        return image

    def read_current_chat_title(self, window: WindowInfo) -> str:
        image = self.capture_region(window, self.title_region(window), "current_chat_title")
        texts: list[str] = []
        for block in self.ocr_blocks(image):
            if block.score >= 0.25 and block.text:
                texts.append(block.text)
        return " ".join(texts).strip()

    def read_search_results_text(self, window: WindowInfo) -> str:
        image = self.capture_region(window, self.search_result_region(window), "search_results")
        texts: list[str] = []
        for block in self.ocr_blocks(image):
            if block.score >= 0.35 and block.text:
                texts.append(block.text)
        return " ".join(texts).strip()

    def read_active_chat_name(self, window: WindowInfo | None = None) -> str:
        active_window = window or self.ensure_ready()
        sidebar_name = self.read_selected_chat_name_from_sidebar(active_window)
        if sidebar_name:
            return sidebar_name
        return self.read_current_chat_title(active_window)

    def read_selected_chat_name_from_sidebar(self, window: WindowInfo) -> str:
        image = self.capture_region(window, self.session_list_region(window), "session_list")
        row_bounds = self.find_selected_session_row(image)
        if row_bounds is None:
            return ""

        row_top, row_bottom = row_bounds
        width, _ = image.size
        row_height = max(1, row_bottom - row_top)
        name_left = int(width * 0.15)
        name_right = int(width * 0.78)
        name_top = max(0, row_top + int(row_height * 0.10))
        name_bottom = min(image.size[1], row_top + int(row_height * 0.52))
        if name_bottom <= name_top or name_right <= name_left:
            return ""

        name_image = image.crop((name_left, name_top, name_right, name_bottom))
        texts: list[str] = []
        for block in sorted(self.ocr_blocks(name_image), key=lambda item: (item.top, item.left)):
            if block.score >= 0.2 and block.text:
                texts.append(block.text)
        return " ".join(texts).strip()

    @staticmethod
    def find_selected_session_row(image: Any) -> tuple[int, int] | None:
        rgb = np.array(image.convert("RGB"))
        if rgb.size == 0:
            return None

        red = rgb[:, :, 0].astype(np.int16)
        green = rgb[:, :, 1].astype(np.int16)
        blue = rgb[:, :, 2].astype(np.int16)
        green_mask = (green >= 120) & (green - red >= 20) & (green - blue >= 20)
        green_counts = green_mask.sum(axis=1)
        row_threshold = max(20, int(rgb.shape[1] * 0.18))
        active_rows = green_counts >= row_threshold
        if not active_rows.any():
            return None

        best_start = -1
        best_end = -1
        start = None
        for index, active in enumerate(active_rows):
            if active and start is None:
                start = index
            elif not active and start is not None:
                if index - start > best_end - best_start:
                    best_start, best_end = start, index
                start = None
        if start is not None and len(active_rows) - start > best_end - best_start:
            best_start, best_end = start, len(active_rows)

        if best_start < 0 or best_end - best_start < 20:
            return None
        return max(0, best_start - 2), min(rgb.shape[0], best_end + 2)

    @staticmethod
    def chat_title_matches(expected_chat_name: str, current_title: str) -> bool:
        expected = canonicalize_chat_name(expected_chat_name)
        current = canonicalize_chat_name(current_title)
        if not expected or not current:
            return False
        return expected in current or current in expected

    def ocr_blocks(self, image: Any) -> list[OcrBlock]:
        result, _ = self.ocr(np.array(image))
        if not result:
            return []

        blocks: list[OcrBlock] = []
        for points, text, score in result:
            normalized_text = normalize_text(str(text))
            normalized_score = float(score)
            if not normalized_text or normalized_text in UI_NOISE_TEXTS:
                continue
            if TIME_RE.match(normalized_text):
                continue

            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            blocks.append(
                OcrBlock(
                    text=normalized_text,
                    score=normalized_score,
                    left=min(xs),
                    top=min(ys),
                    right=max(xs),
                    bottom=max(ys),
                )
            )
        return blocks

    def parse_latest_message(
        self,
        chat_name: str,
        is_group_chat: bool,
        window: WindowInfo,
        image: Any,
        blocks: list[OcrBlock],
    ) -> IncomingMessage | None:
        if not blocks:
            return None

        region = self.message_region(window)
        region_width = region[2] - region[0]
        clusters = build_message_clusters(blocks, region_width)
        if not clusters:
            return None

        for cluster in clusters:
            cluster.side = classify_cluster_side(cluster, image, region_width)

        latest = max(clusters, key=lambda item: item.bottom)
        is_self = latest.side == "right"
        sender = "我"
        content = latest.text

        if is_group_chat and not is_self and len(latest.lines) >= 2:
            sender = latest.lines[0].text
            content = "\n".join(line.text for line in latest.lines[1:]).strip()
        elif not is_self:
            sender = chat_name

        content = content.strip()
        if not content:
            return None

        fingerprint = self.make_fingerprint(
            chat_name,
            sender,
            content,
            is_self,
            int(latest.bottom),
            int(latest.left),
        )
        return IncomingMessage(
            chat_name=chat_name,
            sender=sender,
            content=content,
            is_group_chat=is_group_chat,
            is_self=is_self,
            fingerprint=fingerprint,
        )

    @staticmethod
    def make_fingerprint(
        chat_name: str,
        sender: str,
        content: str,
        is_self: bool,
        bottom: int,
        left: int,
    ) -> str:
        raw = "|".join(
            [
                chat_name,
                sender,
                content,
                str(is_self),
                str(bottom),
                str(left),
            ]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class WeixinGuiBridge:
    def __init__(self, config: BridgeConfig) -> None:
        self.config = config
        self.client = SpringBootClient(config)
        self.controller = WeixinController(config)
        self.processed_cache = ProcessedMessageCache(config.max_processed_messages)
        self.handled_request_cache = ProcessedMessageCache(config.max_processed_messages)
        self.outgoing_text_cache = ProcessedMessageCache(config.max_processed_messages)
        self.listen_contacts = set(config.listen_contacts)
        self.listen_groups = set(config.listen_groups)
        self.targets = list(dict.fromkeys(config.listen_contacts + config.listen_groups))
        self.normalized_target_map = {
            normalize_text(target).lower(): target for target in self.targets if normalize_text(target)
        }
        self.group_trigger_aliases = build_group_trigger_aliases(config.group_trigger_prefixes)

    def run(self) -> None:
        if not self.targets:
            raise ValueError("listen_contacts 和 listen_groups 不能同时为空")

        self.controller.ensure_ready()
        logging.info("Weixin OCR bridge is running")
        logging.info("Listening targets: %s", ", ".join(self.targets))
        while True:
            try:
                self.process_current_chat()
            except KeyboardInterrupt:
                logging.info("Bridge stopped")
                return
            except Exception:
                logging.exception("Unexpected error in polling loop")
            time.sleep(self.config.poll_interval_seconds)

    def process_current_chat(self) -> None:
        window = self.controller.ensure_ready()
        current_chat_name = self.controller.read_active_chat_name(window)
        if not current_chat_name:
            logging.warning("未识别到当前会话名，已跳过本轮")
            return

        target = self.resolve_target_name(current_chat_name)
        if target is None:
            logging.debug("当前会话不在监听名单中: %s", current_chat_name)
            return

        is_group_chat = target in self.listen_groups
        try:
            incoming = self.controller.read_latest_message_from_current_chat(window, target, is_group_chat)
        except Exception:
            logging.exception("Failed to read latest message from %s", target)
            return

        if incoming is None:
            return
        if self.processed_cache.contains(incoming.fingerprint):
            return
        request_fingerprint = self.make_request_fingerprint(incoming)
        if self.handled_request_cache.contains(request_fingerprint):
            self.processed_cache.add(incoming.fingerprint)
            return
        outgoing_fingerprint = self.make_outgoing_text_fingerprint(incoming.chat_name, incoming.content)
        if self.outgoing_text_cache.contains(outgoing_fingerprint):
            logging.info("Skipped outgoing echo in %s: %s", incoming.chat_name, incoming.content)
            self.processed_cache.add(incoming.fingerprint)
            return
        if self.config.ignore_self_messages and incoming.is_self:
            self.processed_cache.add(incoming.fingerprint)
            return

        normalized_text = self.apply_group_trigger(incoming)
        if normalized_text is None:
            if incoming.is_group_chat:
                logging.info("Ignored group message in %s: %s", incoming.chat_name, incoming.content)
            self.processed_cache.add(incoming.fingerprint)
            return

        incoming.content = normalized_text
        logging.info("Received message from %s: %s", incoming.chat_name, incoming.content)
        try:
            if self.try_handle_local_file_request(incoming):
                self.handled_request_cache.add(request_fingerprint)
                self.processed_cache.add(incoming.fingerprint)
                return
            reply = self.client.get_reply(incoming)
            final_reply = f"{self.config.reply_prefix}{reply}".strip()
            self.outgoing_text_cache.add(self.make_outgoing_text_fingerprint(incoming.chat_name, final_reply))
            self.controller.send_text(incoming.chat_name, final_reply)
            self.handled_request_cache.add(request_fingerprint)
            logging.info("Replied to %s", incoming.chat_name)
            self.processed_cache.add(incoming.fingerprint)
        except Exception:
            logging.exception("Failed to reply to %s", incoming.chat_name)

    def resolve_target_name(self, current_chat_name: str) -> str | None:
        normalized = canonicalize_chat_name(current_chat_name)
        if not normalized:
            return None
        exact = self.normalized_target_map.get(normalized)
        if exact is not None:
            return exact
        for normalized_target, original_target in self.normalized_target_map.items():
            if normalized in normalized_target or normalized_target in normalized:
                return original_target
        return None

    @staticmethod
    def make_request_fingerprint(message: IncomingMessage) -> str:
        raw = "|".join(
            [
                normalize_text(message.chat_name).lower(),
                normalize_text(message.sender).lower(),
                normalize_text(message.content).lower(),
                str(message.is_group_chat),
                str(message.is_self),
            ]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def make_outgoing_text_fingerprint(chat_name: str, content: str) -> str:
        raw = "|".join(
            [
                normalize_text(chat_name).lower(),
                normalize_text(content).lower(),
            ]
        )
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def apply_group_trigger(self, message: IncomingMessage) -> str | None:
        if not message.is_group_chat:
            return message.content

        aliases = self.group_trigger_aliases
        if not aliases:
            return message.content

        extracted = extract_triggered_group_content(message.content, aliases)
        if extracted is not None:
            return extracted
        return None

    def try_handle_local_file_request(self, message: IncomingMessage) -> bool:
        if message.is_group_chat:
            if message.chat_name not in self.listen_groups:
                return False
        elif message.chat_name not in self.listen_contacts:
            return False

        requested_file = resolve_requested_file_path(message.content)
        if requested_file is None:
            return False

        if not requested_file.exists() or not requested_file.is_file():
            error_text = f"没有找到文件：{requested_file}"
            self.outgoing_text_cache.add(self.make_outgoing_text_fingerprint(message.chat_name, error_text))
            self.controller.send_text(message.chat_name, error_text)
            logging.warning("Requested file not found: %s", requested_file)
            return True

        self.controller.send_file(message.chat_name, requested_file)
        logging.info("Sent file to %s: %s", message.chat_name, requested_file)
        return True


def normalize_text(value: str) -> str:
    text = value.replace("\u2002", " ").replace("\u2004", " ").replace("\u2005", " ")
    text = text.replace("\u2006", " ").replace("\u2009", " ")
    text = " ".join(text.split())
    return text.strip()


def canonicalize_chat_name(value: str) -> str:
    normalized = normalize_text(value)
    normalized = re.sub(r"\(\s*\d+\s*\)$", "", normalized)
    normalized = re.sub(r"（\s*\d+\s*）$", "", normalized)
    return normalized.strip().lower()


def build_group_trigger_aliases(prefixes: list[str]) -> list[str]:
    aliases: list[str] = []
    for prefix in prefixes:
        normalized = normalize_text(prefix).lstrip("@＠").strip()
        normalized = normalized.strip(":：,，。.!！？?")
        if normalized and normalized not in aliases:
            aliases.append(normalized)
    return aliases


def extract_triggered_group_content(message_text: str, aliases: list[str]) -> str | None:
    normalized = normalize_text(message_text)
    if not normalized:
        return None

    mention_targets = [match.group(1).strip() for match in MENTION_TOKEN_RE.finditer(normalized)]
    if not mention_targets:
        return None

    matched_alias = next(
        (
            alias
            for mention_target in mention_targets
            for alias in aliases
            if normalize_text(mention_target).lower() == normalize_text(alias).lower()
            or normalize_text(mention_target).lower().startswith(normalize_text(alias).lower())
        ),
        None,
    )
    if matched_alias is None:
        return None

    cleaned = normalized
    for alias in aliases:
        cleaned = re.sub(
            rf"[@＠]\s*{re.escape(alias)}",
            " ",
            cleaned,
            flags=re.IGNORECASE,
        )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,，:：")
    return cleaned or normalized


def resolve_requested_file_path(message_text: str) -> Path | None:
    normalized = normalize_text(message_text)
    if not normalized:
        return None

    send_keywords = ("发给我", "发我", "发送", "发一下", "传给我", "把")
    if not any(keyword in normalized for keyword in send_keywords):
        return None

    absolute_match = ABSOLUTE_PATH_RE.search(message_text)
    if absolute_match:
        path = Path(absolute_match.group(1).strip().strip('"').strip("'")).expanduser()
        logging.info("Detected absolute file path request: %s", path)
        return path

    candidate_file_names = extract_candidate_file_names(message_text)
    if not candidate_file_names:
        return None

    candidate_dirs = resolve_candidate_directories(normalized)
    logging.info("Detected file request candidates: names=%s dirs=%s", candidate_file_names, candidate_dirs)
    for filename in candidate_file_names:
        for directory in candidate_dirs:
            candidate = directory / filename
            if candidate.exists():
                return candidate

    if candidate_dirs and candidate_file_names:
        return candidate_dirs[0] / candidate_file_names[0]
    return None


def extract_candidate_file_names(message_text: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for match in FILE_NAME_RE.finditer(message_text):
        raw_candidate = match.group(1).strip().strip('"').strip("'").rstrip("，。,.!！?？")
        for refined in refine_file_name_candidate(raw_candidate):
            normalized = refined.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(refined)
    filtered: list[str] = []
    for candidate in sorted(candidates, key=len):
        if any(candidate.lower().endswith(existing.lower()) for existing in filtered):
            continue
        filtered.append(candidate)
    return filtered


def refine_file_name_candidate(candidate: str) -> list[str]:
    variants: list[str] = []

    def add_variant(value: str) -> None:
        cleaned = value.strip().strip('"').strip("'").rstrip("，。,.!！?？")
        cleaned = cleaned.replace("/", "\\")
        if "\\" in cleaned:
            cleaned = cleaned.split("\\")[-1]
        cleaned = cleaned.strip()
        if "." not in cleaned:
            return
        if re.fullmatch(r"[A-Za-z0-9_\-\u4e00-\u9fa5 .()（）]+\.[A-Za-z0-9]{1,10}", cleaned) is None:
            return
        if cleaned not in variants:
            variants.append(cleaned)

    add_variant(candidate)
    stripped_prefix = FILE_PREFIX_RE.sub("", candidate).strip()
    if stripped_prefix and stripped_prefix != candidate:
        add_variant(stripped_prefix)
    for splitter in ("的", " ", "“", "”", "\"", "'"):
        parts = [part for part in candidate.split(splitter) if part.strip()]
        if parts:
            add_variant(parts[-1])
            stripped_part = FILE_PREFIX_RE.sub("", parts[-1]).strip()
            if stripped_part and stripped_part != parts[-1]:
                add_variant(stripped_part)

    if variants:
        variants.sort(key=len)
    return variants


def resolve_candidate_directories(message_text: str) -> list[Path]:
    home = Path.home()
    candidates: list[Path] = []
    desktop_candidates = [home / "Desktop", home / "OneDrive" / "Desktop"]
    documents_candidates = [home / "Documents", home / "OneDrive" / "Documents"]
    downloads_candidates = [home / "Downloads", home / "OneDrive" / "Downloads"]

    if "桌面" in message_text:
        candidates.extend(desktop_candidates)
    if "文档" in message_text or "documents" in message_text.lower():
        candidates.extend(documents_candidates)
    if "下载" in message_text or "downloads" in message_text.lower():
        candidates.extend(downloads_candidates)
    if not candidates:
        candidates.extend(desktop_candidates)
        candidates.extend(documents_candidates)
        candidates.extend(downloads_candidates)

    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate).lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(candidate)
    return result


def copy_files_to_clipboard(file_paths: list[Path]) -> None:
    if not file_paths:
        raise ValueError("file_paths cannot be empty")

    resolved_paths = [str(path.resolve()) for path in file_paths]
    files_buffer = ("\0".join(resolved_paths) + "\0\0").encode("utf-16le")
    dropfiles = DROPFILES()
    dropfiles.pFiles = ctypes.sizeof(DROPFILES)
    dropfiles.pt_x = 0
    dropfiles.pt_y = 0
    dropfiles.fNC = False
    dropfiles.fWide = True
    data = bytes(dropfiles) + files_buffer

    handle = kernel32.GlobalAlloc(DROPFILES_GHND, len(data))
    if not handle:
        raise OSError(f"GlobalAlloc failed, last_error={ctypes.get_last_error()}")
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        raise OSError(f"GlobalLock failed, last_error={ctypes.get_last_error()}")

    try:
        ctypes.memmove(pointer, data, len(data))
    finally:
        kernel32.GlobalUnlock(handle)

    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(handle)
        raise OSError(f"OpenClipboard failed, last_error={ctypes.get_last_error()}")

    try:
        if not user32.EmptyClipboard():
            raise OSError(f"EmptyClipboard failed, last_error={ctypes.get_last_error()}")
        if not user32.SetClipboardData(CF_HDROP, handle):
            raise OSError(f"SetClipboardData failed, last_error={ctypes.get_last_error()}")
        handle = None
    finally:
        user32.CloseClipboard()
        if handle:
            kernel32.GlobalFree(handle)


def build_message_clusters(blocks: list[OcrBlock], region_width: int) -> list[MessageCluster]:
    clusters: list[MessageCluster] = []
    for block in sorted(blocks, key=lambda item: (item.top, item.left)):
        if block.score < 0.35:
            continue

        side = infer_message_side(block, region_width)
        if not clusters:
            clusters.append(MessageCluster(side=side, lines=[block]))
            continue

        current = clusters[-1]
        previous = current.lines[-1]
        vertical_gap = block.top - previous.bottom
        horizontal_gap = abs(block.left - previous.left)
        same_side = current.side == side
        same_bubble = same_side and vertical_gap <= 36 and horizontal_gap <= 140

        if same_bubble:
            current.lines.append(block)
        else:
            clusters.append(MessageCluster(side=side, lines=[block]))
    return clusters


def infer_message_side(block: OcrBlock, region_width: int) -> str:
    left_gap = max(0.0, block.left)
    right_gap = max(0.0, region_width - block.right)

    if block.right >= region_width * 0.80:
        return "right"
    if block.left <= region_width * 0.20:
        return "left"
    return "right" if right_gap < left_gap else "left"


def classify_cluster_side(cluster: MessageCluster, image: Any, region_width: int) -> str:
    position_side = infer_message_side(
        OcrBlock(
            text="",
            score=1.0,
            left=min(line.left for line in cluster.lines),
            top=min(line.top for line in cluster.lines),
            right=max(line.right for line in cluster.lines),
            bottom=max(line.bottom for line in cluster.lines),
        ),
        region_width,
    )
    green_ratio = estimate_cluster_green_ratio(cluster, image)
    if green_ratio >= 0.10:
        return "right"
    return position_side


def estimate_cluster_green_ratio(cluster: MessageCluster, image: Any) -> float:
    rgb = np.array(image.convert("RGB"))
    if rgb.size == 0:
        return 0.0

    left = max(0, int(min(line.left for line in cluster.lines)) - 20)
    top = max(0, int(min(line.top for line in cluster.lines)) - 16)
    right = min(rgb.shape[1], int(max(line.right for line in cluster.lines)) + 20)
    bottom = min(rgb.shape[0], int(max(line.bottom for line in cluster.lines)) + 16)
    if right <= left or bottom <= top:
        return 0.0

    crop = rgb[top:bottom, left:right]
    red = crop[:, :, 0].astype(np.int16)
    green = crop[:, :, 1].astype(np.int16)
    blue = crop[:, :, 2].astype(np.int16)
    green_mask = (green >= 145) & (green - red >= 10) & (green - blue >= 10)
    return float(green_mask.sum()) / float(crop.shape[0] * crop.shape[1])


def safe_filename(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Weixin 4.x OCR bridge for Spring Boot chatbot")
    parser.add_argument("--config", default="config.json", help="Path to the bridge config file")
    return parser.parse_args()


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def main() -> None:
    configure_logging()
    args = parse_args()
    raw_config_path = Path(args.config)
    if raw_config_path.is_absolute():
        config_path = raw_config_path
    elif raw_config_path.exists():
        config_path = raw_config_path.resolve()
    else:
        config_path = Path(__file__).resolve().parent / raw_config_path

    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {config_path}. Copy config.example.json to config.json first."
        )

    config = BridgeConfig.load(config_path)
    bridge = WeixinGuiBridge(config)
    bridge.run()


if __name__ == "__main__":
    main()
