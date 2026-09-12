"""Published policy validation and immutable runtime snapshots, with temporary stores."""
import os
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from wechat_agent.execution_policy import ExecutionPolicy
from wechat_agent.config import Settings, ConfigurationError, _admin_active_agent_config
from wechat_agent.admin.events import AdminEventRecorder
from wechat_agent.admin.security import SecretBox
from wechat_agent.admin.store import AdminStore, ConflictError


class PolicyConfigurationTests(unittest.TestCase):
    def test_strict_schema_and_no_unreviewed_allow(self):
        for data in ({'default_action': 'allow'}, {'rules': {'bash': 'allow'}},
                     {'rules': {'*': 'deny'}}, {'version': True}, {'confirmation_timeout_seconds': 91},
                     {'confirmation_timeout_seconds': True}, {'extra': 1},
                     {'rules': {'mcp__desktop__request_confirmation': 'deny'}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                ExecutionPolicy.parse(data)
        self.assertEqual('deny', ExecutionPolicy.parse({'shell': False}).action('bash'))
        self.assertEqual('ask', ExecutionPolicy.parse({'shell': True}).action('bash'))

    def test_policy_is_detached_and_immutable(self):
        data = {'rules': {'bash': 'deny'}}
        policy = ExecutionPolicy.parse(data)
        data['rules']['bash'] = 'ask'
        self.assertEqual('deny', policy.action('bash'))
        with self.assertRaises(FrozenInstanceError):
            policy.default_action = 'allow'

    def test_store_publish_does_not_change_running_snapshot_and_rollback_preserves_policy(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = AdminStore(root/'admin.db', SecretBox.load(root/'key'))
            profile = store.create_config_profile('policy', '', 'admin', None)
            body = dict(provider='deepseek', model='test', system_prompt='test', request_timeout_seconds=450,
                        task_timeout_seconds=480, tool_policy={'confirmation_timeout_seconds': 30, 'rules': {'bash': 'deny'}})
            first = store.create_config_revision(profile['id'], body, 'admin', None)
            store.publish_config_revision(profile['id'], first['id'], 'admin', None)
            active = store.get_active_runtime_config()
            snapshot = ExecutionPolicy.parse(active['tool_policy'])
            second = store.create_config_revision(profile['id'], {**body, 'tool_policy': {}}, 'admin', None)
            store.publish_config_revision(profile['id'], second['id'], 'admin', None)
            self.assertEqual('deny', snapshot.action('bash'))
            self.assertEqual(30, snapshot.confirmation_timeout_seconds)
            self.assertEqual('ask', ExecutionPolicy.parse(store.get_active_runtime_config()['tool_policy']).action('bash'))
            restored = store.rollback_config(profile['id'], first['id'], 'admin', None)
            self.assertEqual('deny', ExecutionPolicy.parse(restored['tool_policy']).action('bash'))
            with store.transaction() as db:
                db.execute('UPDATE config_revisions SET tool_policy_json=? WHERE id=?', ('{"rules":{"bash":"allow"}}', second['id']))
            with self.assertRaises(ConflictError):
                store.publish_config_revision(profile['id'], second['id'], 'admin', None)
            self.assertEqual(restored['id'], store.get_active_runtime_config()['id'])
            with store.transaction() as db:
                db.execute("UPDATE system_settings SET value_json=? WHERE key='active_agent_config'", ('{"revision_id":"missing"}',))
            with self.assertRaises(ConflictError):
                store.get_active_runtime_config()

    def test_unavailable_managed_configuration_does_not_fall_back(self):
        with patch.dict(os.environ, {'ADMIN_MANAGED_AGENT_CONFIG': 'true'}), patch(
                'wechat_agent.admin.events.get_event_recorder', return_value=AdminEventRecorder(None)):
            with self.assertRaises(ConfigurationError):
                _admin_active_agent_config()

    def test_settings_read_published_policy(self):
        published = {'system_prompt': 'test', 'task_timeout_seconds': 480, 'request_timeout_seconds': 450,
                     'tool_policy': {'confirmation_timeout_seconds': 25, 'default_action': 'deny'}}
        with patch.dict(os.environ, {'WECHAT_BOT_ID': 'test', 'WECHAT_BOT_SECRET': 'test',
                                    'HARNESS_ENABLED': 'false', 'DESKTOP_TOOLS_ENABLED': 'false'}, clear=True), \
                patch('wechat_agent.config.load_dotenv'), \
                patch('wechat_agent.config._admin_active_agent_config', return_value=published), \
                patch('wechat_agent.config._admin_active_connection', return_value=None), \
                patch('wechat_agent.config._discover_doubao_launcher', return_value=None), \
                patch('wechat_agent.config.Path.home', return_value=Path.cwd()):
            settings = Settings.from_environment()
            self.assertEqual('deny', settings.execution_policy.action('unknown'))
            self.assertEqual(25, settings.execution_policy.confirmation_timeout_seconds)
