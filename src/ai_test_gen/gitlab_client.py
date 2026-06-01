"""Push a generated test to GitLab as a new branch and open a merge request.

Phase 1.D — tasks ``6i6kv7d`` (MR opener) + ``1ich5gw`` (heal-attempt summaries in
the MR). Per AI_TEST_GENERATION_GUIDE.md §3.12, with two improvements:

- **Collision-resistant branch name** (`ai-gen/<key>-<utc>-<suffix>`): the CI job id
  when running in CI, else a short random token — two runs for the same Jira key in
  the same second won't collide.
- **Heal-attempt transparency**: the heal count, final status, and each Healer
  ``changes_summary`` are rendered into the MR description so reviewers can spot tests
  that needed multiple rounds.

There is intentionally **no credential-leak scan** (2026-06-01 decision): generated
tests embed the disposable staging dummy logins from ``project_context.md`` as literals.
The review checklist asks reviewers to confirm no *real* credentials/PII slipped in.
"""
from __future__ import annotations

import os
import secrets
from datetime import UTC, datetime

import gitlab

from .config import Config
from .models import GeneratedTest, TestPlan

MR_LABELS = ["ai-generated", "qa-review-needed"]


class GitLabClient:
    """Thin wrapper over python-gitlab for opening AI-generated-test MRs."""

    def __init__(self, config: Config):
        self.config = config
        self.gl = gitlab.Gitlab(config.gitlab_base_url, private_token=config.gitlab_token)
        self.project = self.gl.projects.get(config.gitlab_project_id)

    def open_mr(
        self,
        test: GeneratedTest,
        plan: TestPlan,
        test_case_key: str,
        *,
        plan_json: str | None = None,
        heal_summaries: list[str] | None = None,
        heal_attempts: int = 0,
        final_status: str | None = None,
    ) -> str:
        """Create a branch, commit the test + plan JSON, open an MR. Returns the MR web URL.

        ``plan_json`` is the serialized plan to commit (the orchestrator passes the
        context-hash-enriched JSON so the committed copy matches the local one); falls
        back to ``plan.model_dump_json`` when omitted.
        """
        branch_name = _branch_name(test_case_key)
        actions = [
            {
                "action": "create",
                "file_path": f"tests/generated/{test.file_name}",
                "content": test.code,
            },
            {
                "action": "create",
                "file_path": f"tests/generated/_plans/{test_case_key}.json",
                "content": plan_json or plan.model_dump_json(indent=2),
            },
        ]

        self.project.branches.create(
            {"branch": branch_name, "ref": self.config.gitlab_target_branch}
        )
        self.project.commits.create(
            {
                "branch": branch_name,
                "commit_message": f"AI-generated test for {test_case_key}: {test.description}",
                "actions": actions,
            }
        )
        mr = self.project.mergerequests.create(
            {
                "source_branch": branch_name,
                "target_branch": self.config.gitlab_target_branch,
                "title": f"[AI] {test_case_key}: {test.description}",
                "description": _build_mr_description(
                    test,
                    plan,
                    test_case_key,
                    heal_summaries=heal_summaries,
                    heal_attempts=heal_attempts,
                    final_status=final_status,
                ),
                "labels": MR_LABELS,
                "remove_source_branch": True,
            }
        )
        return mr.web_url


def _branch_name(test_case_key: str) -> str:
    """`ai-gen/<key>-<utc>-<suffix>` — CI job id when available, else a random token."""
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    suffix = os.environ.get("CI_JOB_ID") or secrets.token_hex(3)
    return f"ai-gen/{test_case_key.lower()}-{timestamp}-{suffix}"


def _build_mr_description(
    test: GeneratedTest,
    plan: TestPlan,
    key: str,
    *,
    heal_summaries: list[str] | None,
    heal_attempts: int,
    final_status: str | None,
) -> str:
    healer_block = ""
    if heal_attempts > 0:
        bullets = "\n".join(f"- {s}" for s in (heal_summaries or [])) or "- (no summary recorded)"
        healer_block = f"\n### Healer attempts ({heal_attempts})\n{bullets}\n"

    return f"""## AI-Generated Playwright Test

**Source Jira ticket:** `{key}`
**Final run status:** `{final_status or "unknown"}` · **Heal attempts:** {heal_attempts}

### What this test does
{test.description}

### Test plan summary
- Target URL: `{plan.target_url}`
- Steps: {len(plan.steps)}
- Preconditions: {len(plan.preconditions)}

### Planner notes
{plan.notes or "(none)"}
{healer_block}
### Review checklist
- [ ] Test name and description are accurate
- [ ] Selectors are stable (IDs preferred)
- [ ] Assertions cover the expected outcomes from the original test case
- [ ] No **real** credentials or PII (the staging dummy logins from project_context are expected)
- [ ] Test runs successfully locally
- [ ] Test is idempotent (can run multiple times without side effects)

---
*Opened by the AI test generation pipeline. The plan JSON is committed at \
`tests/generated/_plans/{key}.json` for traceability.*
"""
