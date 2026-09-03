"""Initialize the local Harness runtime and optionally execute one smoke turn."""

import argparse
from dataclasses import replace
from uuid import uuid4

from wechat_agent.adapters.deepseek_harness import _create_harness
from wechat_agent.config import Settings


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prompt",
        help="Optional real model prompt. Omit it for an initialization-only check.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20,
        help="Harness request timeout used by this smoke process.",
    )
    return parser.parse_args()


def main() -> None:
    arguments = _arguments()
    settings = replace(
        Settings.from_environment(),
        harness_request_timeout_seconds=arguments.timeout,
    )
    harness = _create_harness(settings)
    try:
        harness.start()
        if not arguments.prompt:
            print("HARNESS_RUNTIME_INIT_OK")
            return
        result = harness.run(
            arguments.prompt,
            session_id=f"wecom-smoke-{uuid4().hex[:24]}",
        )
        finish_reason = getattr(result, "finish_reason", None)
        response = str(getattr(result, "final_response", "") or "").strip()
        if finish_reason not in {"completed", "max-tokens"} or not response:
            raise RuntimeError(
                "Harness smoke turn failed: "
                f"finish_reason={finish_reason!r}, response_present={bool(response)}"
            )
        print(f"HARNESS_RUNTIME_PROMPT_OK finish_reason={finish_reason}")
        print(response)
    finally:
        harness.close()


if __name__ == "__main__":
    main()
