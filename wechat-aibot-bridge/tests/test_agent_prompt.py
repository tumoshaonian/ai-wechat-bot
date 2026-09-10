"""Prompt policy must not turn sample applications into a software whitelist."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from wechat_agent.config import Settings


class AgentPromptTests(unittest.TestCase):
    def test_default_and_published_prompts_include_application_independent_policy(self):
        for published in (None, {"system_prompt": "你是项目助手", "task_timeout_seconds": 480, "request_timeout_seconds": 480}):
            with self.subTest(published=bool(published)), patch.dict(os.environ, {
                "WECHAT_BOT_ID": "test-only", "WECHAT_BOT_SECRET": "test-only",
                "HARNESS_ENABLED": "false", "DESKTOP_TOOLS_ENABLED": "false",
            }, clear=True), patch("wechat_agent.config.load_dotenv"), patch(
                "wechat_agent.config._admin_active_agent_config", return_value=published,
            ), patch("wechat_agent.config._admin_active_connection", return_value=None), patch(
                "wechat_agent.config._discover_doubao_launcher", return_value=None,
            ), patch("wechat_agent.config.Path.home", return_value=Path.cwd()):
                prompt = Settings.from_environment().harness_system_prompt
                self.assertIn("不按软件名称硬编码任务路由", prompt)
                self.assertIn("没有注册视觉能力", prompt)
                self.assertNotIn("用户要求在豆包中提问时", prompt)
