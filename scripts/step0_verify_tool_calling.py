"""
Step 0 — verify the internal LLM gateway can do tool calling.

This is the single most important pre-flight check for the whole pipeline. Many
on-prem gateways don't properly proxy `tools` / `tool_choice` even when the
underlying model supports function calling. Find out now, not at week 3.

Reads from .env:
  LLM_BASE_URL   — OpenAI-compatible endpoint
  LLM_API_KEY    — gateway secret

Must run on the company laptop (the gateway is not reachable from the private PC).

Run:
  uv run python scripts/step0_verify_tool_calling.py

Source: AI_TEST_GENERATION_GUIDE.md §3.2.
"""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

if not os.environ.get("LLM_BASE_URL") or not os.environ.get("LLM_API_KEY"):
    print("[fail] LLM_BASE_URL and LLM_API_KEY must be set in .env")
    sys.exit(2)

client = OpenAI(base_url=os.environ["LLM_BASE_URL"], api_key=os.environ["LLM_API_KEY"])

# A dummy tool the model can call. Deliberately simple so failure means the
# gateway/model can't do tool calling, not that the tool was too complex.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"},
                },
                "required": ["city"],
            },
        },
    }
]

MODELS_TO_TEST = [
    "openai/gpt-oss-120b",
    "mistralai/devstral-small-2-2512",
    "meta-llama/llama-3.3-70b-instruct",
]


def test_model(model: str) -> bool:
    print(f"\n=== Testing {model} ===")
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": "What's the weather in Zagreb? Use the tool."}
            ],
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=200,
        )
        msg = resp.choices[0].message

        if not msg.tool_calls:
            print(f"  [fail] Model did not call any tool. Response: {msg.content!r}")
            return False

        call = msg.tool_calls[0]
        print(f"  [ok] Tool called: {call.function.name}")
        print(f"  [ok] Arguments:   {call.function.arguments}")
        return True
    except Exception as e:
        print(f"  [fail] Error: {e}")
        return False


def main() -> int:
    results = {m: test_model(m) for m in MODELS_TO_TEST}
    print("\n=== Summary ===")
    for m, ok in results.items():
        print(f"  {'[ok]  ' if ok else '[fail]'} {m}")
    if not all(results.values()):
        print(
            "\nWARNING: at least one model failed tool calling. Stop and investigate "
            "before proceeding.\n"
            "Common causes:\n"
            "  - gateway not forwarding `tools` parameter\n"
            "  - model deployment lacking function-calling fine-tune\n"
            "  - wrong model name (try GET /v1/models)\n"
            "  - some gateways forward `tools` but not `tool_choice` "
            '(try removing tool_choice="auto")\n'
            "  - gateway may need a custom header (X-Use-Tools: true, etc.) "
            "— ask the platform team"
        )
        return 1
    print("\nAll candidate models pass tool calling. Safe to proceed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
