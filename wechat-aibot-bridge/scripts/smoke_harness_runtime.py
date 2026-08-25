"""Initialize and stop the local Harness runtime without calling a model."""

from dataclasses import replace

from wechat_agent.adapters.deepseek_harness import _create_harness
from wechat_agent.config import Settings


def main() -> None:
    settings = replace(
        Settings.from_environment(),
        harness_request_timeout_seconds=20,
    )
    harness = _create_harness(settings)
    try:
        harness.start()
        print("HARNESS_RUNTIME_INIT_OK")
    finally:
        harness.close()


if __name__ == "__main__":
    main()
