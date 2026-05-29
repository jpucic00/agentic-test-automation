#!/usr/bin/env python3
"""PreToolUse guard: force explicit approval for dependency-mutating actions.

Reads the Claude Code PreToolUse payload from stdin. When a tool call would add
or change a project dependency, it returns permissionDecision="ask" so a human
stays the gatekeeper. Every other call passes through untouched (no output).

Why this exists: the repo is authored on a private PC but installed on a company
laptop. A typosquatted, hallucinated, or compromised package must not slip in
silently. The hash-pinned uv.lock already guarantees install *integrity*; this
guard adds a *trust* checkpoint on the decision to add a package in the first
place. It never blocks (no "deny") — it only forces a conscious approval.

Pure stdlib so it runs under any `python3`; it does not need the project venv.
"""
import json
import sys

# Bash command fragments that install or mutate dependencies. Matched as
# substrings against the whitespace-normalized command, so chained invocations
# like `cd sub && uv add x` are caught, not just commands that start with them.
DEP_COMMAND_MARKERS = (
    "uv add",
    "uv remove",
    "uv lock",          # re-resolves and rewrites uv.lock outside the Edit tool
    "uv pip install",
    "pip install",
    "pip3 install",
    "poetry add",
    "poetry remove",
    "conda install",
)

# Manifest / lockfiles whose direct edits change the dependency set.
DEP_FILES = ("pyproject.toml", "uv.lock")

REASON = (
    "Dependency change detected. Before approving, verify: "
    "(1) it is actually needed (stdlib or an existing dep may cover it); "
    "(2) the EXACT package name is a real, widely-used package on PyPI — "
    "guard against typosquats and hallucinated names; "
    "(3) the version is intended. This repo is installed on a company laptop, "
    "so an unvetted package ships there too."
)


def ask(reason):
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # fail open: a malformed payload defers to the normal permission flow

    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}

    if tool == "Bash":
        command = " ".join(str(tool_input.get("command", "")).split())
        if any(marker in command for marker in DEP_COMMAND_MARKERS):
            ask(REASON)
    else:  # Edit / Write / MultiEdit / NotebookEdit
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        name = str(path).replace("\\", "/").rsplit("/", 1)[-1]
        if name in DEP_FILES:
            ask(f"Edit to {name}: {REASON}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
