"""
Step 0d — verify the gateway HONORS the ``reasoning_effort`` request param.

OpenAI-compatible gateways commonly accept unknown request params and silently
drop them. If you set ``PLANNER_REASONING_EFFORT`` / ``HEALER_REASONING_EFFORT``
without this check, an ignored setting masquerades as a tuned pipeline. This
script sends the SAME prompt at ``low`` and ``high`` effort and compares token
usage: an honoring gateway deliberates materially longer at high effort; a
dropping one produces near-identical usage.

Reads from .env (same connection policy as the other step0 scripts):
  LLM_BASE_URL / LLM_API_KEY   — OpenAI-compatible endpoint + secret
  PLANNER_MODEL                — model to probe (default openai/gpt-oss-120b)
  MTLS_* / SSL_CERT_FILE / USE_HTTP_PROXY — optional, see .env.example

Run (on a machine that can reach the gateway):
  uv run python scripts/step0d_verify_reasoning_effort.py
  uv run python scripts/step0d_verify_reasoning_effort.py --model openai/gpt-oss-120b

Exit codes: 0 = HONORED · 1 = NOT honored · 2 = error/inconclusive.
Only set the *_REASONING_EFFORT env vars after a 0 exit.
"""
from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv
from openai import DefaultHttpxClient, OpenAI

load_dotenv()

# Local mTLS helper; must be imported AFTER load_dotenv() because it reads env.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _mtls  # noqa: E402

from ai_test_gen.llm import judge_reasoning_effort_support  # noqa: E402

# Hard enough that a reasoning model's deliberation length visibly scales with
# effort, small enough to stay cheap.
PROBE_PROMPT = (
    "How many distinct ways can you make change for 60 cents using pennies, "
    "nickels, dimes and quarters? Answer with just the number."
)


def _client() -> OpenAI:
    if not os.environ.get("LLM_BASE_URL") or not os.environ.get("LLM_API_KEY"):
        print("[fail] LLM_BASE_URL and LLM_API_KEY must be set in .env")
        sys.exit(2)
    http_kwargs: dict = {
        "trust_env": _mtls.get_trust_env(),
        "verify": _mtls.get_verify_arg(),
    }
    cert = _mtls.get_cert_arg()
    if cert is not None:
        http_kwargs["cert"] = cert
    return OpenAI(
        base_url=os.environ["LLM_BASE_URL"],
        api_key=os.environ["LLM_API_KEY"],
        http_client=DefaultHttpxClient(**http_kwargs),
    )


def _probe(client: OpenAI, model: str, effort: str) -> int | None:
    """Tokens spent answering the probe at ``effort`` (reasoning tokens preferred)."""
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": PROBE_PROMPT}],
        reasoning_effort=effort,  # type: ignore[arg-type]  # the param under test
    )
    usage = resp.usage
    if usage is None:
        return None
    details = getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None) if details else None
    tokens = reasoning or usage.completion_tokens
    print(f"  effort={effort:<6} -> {tokens} tokens ({'reasoning' if reasoning else 'completion'})")
    return tokens


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=os.environ.get("PLANNER_MODEL", "openai/gpt-oss-120b"),
        help="Model to probe (default: PLANNER_MODEL from .env, else openai/gpt-oss-120b).",
    )
    args = parser.parse_args()

    print(f"mTLS: {_mtls.describe()}")
    print(f"Proxy: {_mtls.describe_trust_env()}")
    print(f"\n=== Probing reasoning_effort on {args.model} ===")

    client = _client()
    try:
        low = _probe(client, args.model, "low")
        high = _probe(client, args.model, "high")
    except Exception as e:
        print(f"\n[fail] ERROR — the gateway rejected the probe: {type(e).__name__}: {e}")
        print("The reasoning_effort param is NOT usable on this gateway/model.")
        return 2

    verdict = judge_reasoning_effort_support(low, high)
    print()
    if verdict == "honored":
        print("[ok] HONORED — high effort deliberates materially longer than low.")
        print("Safe to set PLANNER_REASONING_EFFORT / HEALER_REASONING_EFFORT.")
        return 0
    if verdict == "not-honored":
        print("[fail] NOT honored — usage is near-identical at low vs high effort.")
        print("The gateway silently drops the param. Leave the *_REASONING_EFFORT vars unset.")
        return 1
    print("[fail] INCONCLUSIVE — the gateway returned no usable token usage.")
    print("Cannot prove support; leave the *_REASONING_EFFORT vars unset.")
    return 2


if __name__ == "__main__":
    sys.exit(main())
