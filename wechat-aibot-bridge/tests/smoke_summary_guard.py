"""Boot the actual SDK composition and attempt a tool call without a model."""
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from wechat_agent.adapters.deepseek_harness import _create_harness
from wechat_agent.config import Settings


def main():
    settings = Settings.from_environment()
    bridge = Path(__file__).resolve().parents[1]
    with TemporaryDirectory(prefix="wecom-guard-smoke-") as temporary:
        root = Path(temporary)
        patch = root / "guard.patch.yml"
        patch.write_text("- insert:\n    - id: summary-no-tools\n      name: " + json.dumps((bridge / "config" / "summary-no-tools.mjs").as_uri())
            + "\n    - id: summary-probe\n      name: " + json.dumps((bridge / "tests" / "summary-guard-probe.mjs").as_uri()) + "\n", encoding="utf-8")
        settings = replace(settings, harness_patch_files=(patch,), desktop_tools_enabled=False, harness_dsh_home=root / "home", harness_session_root=root / "journal", harness_workspace=root)
        proof = root / "proof.json"
        harness = _create_harness(settings, delivery_env={"WECOM_GUARD_PROOF": str(proof)})
        try:
            harness.start()
            value = json.loads(proof.read_text(encoding="utf-8"))
            assert value["executed"] is False
            assert "History summarization is text-only" in json.dumps(value)
            print(json.dumps(value, ensure_ascii=False))
        finally:
            harness.close()


if __name__ == "__main__":
    main()
