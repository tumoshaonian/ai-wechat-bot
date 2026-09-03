import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from wechat_agent.session_registry import HarnessSessionRegistry


class HarnessSessionRegistryTests(unittest.TestCase):
    def test_reuses_generation_until_explicit_rotation(self) -> None:
        with TemporaryDirectory() as temporary:
            registry = HarnessSessionRegistry(Path(temporary))

            first = registry.begin("wecom:single:owner")
            registry.finish(first)
            second = registry.begin("wecom:single:owner")
            registry.finish(second)
            rotated = registry.rotate("wecom:single:owner", reason="ended")
            third = registry.begin("wecom:single:owner")

            self.assertEqual(first.session_id, second.session_id)
            self.assertEqual(rotated.generation, 2)
            self.assertTrue(third.session_id.endswith("-g0002"))

    def test_unclean_running_generation_is_automatically_isolated(self) -> None:
        with TemporaryDirectory() as temporary:
            registry = HarnessSessionRegistry(Path(temporary))

            abandoned = registry.begin("wecom:single:owner")
            recovered = registry.begin("wecom:single:owner")

            self.assertTrue(abandoned.session_id.endswith("-g0001"))
            self.assertTrue(recovered.session_id.endswith("-g0002"))
            self.assertTrue(recovered.recovered_interrupted_session)

    def test_registry_never_persists_raw_chat_id(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = HarnessSessionRegistry(root)
            registry.begin("wecom:single:secret-user")

            content = (root / "bridge-conversations.json").read_text(encoding="utf-8")
            self.assertNotIn("secret-user", content)

    def test_clean_bridge_restart_resumes_the_same_durable_session(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_registry = HarnessSessionRegistry(root)
            first = first_registry.begin("wecom:single:owner")
            first_registry.finish(first)

            restarted_registry = HarnessSessionRegistry(root)
            restarted = restarted_registry.begin("wecom:single:owner")

            self.assertEqual(first.session_id, restarted.session_id)
            self.assertTrue(restarted.session_id.endswith("-g0001"))
            self.assertFalse(restarted.recovered_interrupted_session)


if __name__ == "__main__":
    unittest.main()
