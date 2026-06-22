"""Push a generated test to GitLab as a new branch and open a merge request.

Phase 1.D — tasks ``6i6kv7d`` (MR opener) + ``1ich5gw`` (heal-attempt summaries in
the MR). Per AI_TEST_GENERATION_GUIDE.md §3.12, with three improvements:

- **Collision-resistant branch name** (`ai-gen/<key>-<utc>-<suffix>`): the CI job id
  when running in CI, else a short random token — two runs for the same Jira key in
  the same second won't collide.
- **One commit per attempt**: the initial generation, the optional compile-retry, and
  each heal are committed separately to the *same* test file path, so a reviewer can open
  the MR's commit view and diff one attempt against the next (see ``TestRevision``).
- **Heal-attempt transparency**: the heal count, final status, and each Healer
  ``changes_summary`` are rendered into the MR description so reviewers can spot tests
  that needed multiple rounds.

There is intentionally **no credential-leak scan** (2026-06-01 decision): generated
tests embed the disposable staging dummy logins from ``project_context.md`` as literals.
The review checklist asks reviewers to confirm no *real* credentials/PII slipped in.
"""
from __future__ import annotations

import contextlib
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

import gitlab

from . import mtls
from .config import Config
from .models import GeneratedTest, TestPlan

MR_LABELS = ["ai-generated", "qa-review-needed"]


@dataclass(frozen=True)
class TestRevision:
    """One commit in the MR: a labeled revision of the generated test file.

    The orchestrator produces one per code-producing attempt (initial generation, the
    optional compile-retry regeneration, each heal). ``open_mr`` commits them in order to
    the SAME file path, so GitLab's commit view shows the diff from one attempt to the
    next — that's how a reviewer compares heal attempts. ``message`` is the commit message
    (subject, optionally followed by a body); ``code`` is the full file content at that
    attempt.
    """

    message: str
    code: str


class GitLabClient:
    """Thin wrapper over python-gitlab for opening AI-generated-test MRs."""

    def __init__(self, config: Config):
        self.config = config
        # Only constructed when GitLab is enabled; assert so a misconfigured caller
        # fails loudly instead of passing None into python-gitlab.
        assert config.gitlab_base_url and config.gitlab_token and config.gitlab_project_id, (
            "GitLabClient requires GITLAB_BASE_URL/TOKEN/PROJECT_ID "
            "(set GITLAB_ENABLED=false to run without opening an MR)"
        )
        self.gl = gitlab.Gitlab(config.gitlab_base_url, private_token=config.gitlab_token)
        # python-gitlab owns its requests.Session (no session= kwarg in 8.x); apply the
        # gateway proxy/CA policy to it so GitLab calls also go direct over the VPN —
        # requests otherwise honors env HTTP(S)_PROXY and hits the same connection drop.
        mtls.apply_requests_policy(self.gl.session)
        self.project = self.gl.projects.get(config.gitlab_project_id)

    def open_mr(
        self,
        test: GeneratedTest,
        plan: TestPlan,
        test_case_key: str,
        *,
        revisions: list[TestRevision] | None = None,
        plan_json: str | None = None,
        heal_summaries: list[str] | None = None,
        heal_attempts: int = 0,
        final_status: str | None = None,
        trace_path: str | None = None,
    ) -> str:
        """Create a branch, commit one revision per attempt + the plan JSON, open an MR.

        ``revisions`` is the ordered per-attempt history of the test code (initial
        generation → optional compile-retry → each heal). Each becomes its OWN commit
        writing the same ``tests/generated/<file>`` path, so the MR's commit view shows the
        diff from one attempt to the next — that is how a reviewer compares heal attempts.
        When omitted, a single commit with ``test.code`` is made. The plan JSON is committed
        once, in the first commit. ``test.file_name`` is the committed path and
        ``test.description`` titles the MR.

        ``plan_json`` is the serialized plan to commit (the orchestrator passes the
        context-hash-enriched JSON so the committed copy matches the local one); falls
        back to ``plan.model_dump_json`` when omitted.
        """
        branch_name = _branch_name(test_case_key)
        file_path = f"tests/generated/{test.file_name}"
        plan_path = f"tests/generated/_plans/{test_case_key}.json"
        plan_content = plan_json or plan.model_dump_json(indent=2)
        revs = revisions or [
            TestRevision(
                message=f"AI-generated test for {test_case_key}: {test.description}",
                code=test.code,
            )
        ]

        self.project.branches.create(
            {"branch": branch_name, "ref": self.config.gitlab_target_branch}
        )
        try:
            self._commit_revisions(branch_name, file_path, plan_path, plan_content, revs)
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
                        trace_path=trace_path,
                    ),
                    "labels": MR_LABELS,
                    "remove_source_branch": True,
                }
            )
        except Exception:
            # Don't leave an orphan branch behind if a commit or MR creation fails.
            with contextlib.suppress(Exception):
                self.project.branches.delete(branch_name)
            raise
        return mr.web_url

    def _commit_revisions(
        self,
        branch_name: str,
        file_path: str,
        plan_path: str,
        plan_content: str,
        revisions: list[TestRevision],
    ) -> None:
        """Commit each revision in order to ``file_path`` — one commit per attempt.

        The first commit ``create``s the test file and the plan JSON; every later commit
        ``update``s the test file in place, so consecutive commits diff cleanly in the MR.
        A revision whose code is identical to the previously committed one is skipped:
        GitLab rejects a commit with an empty diff, and an unchanged attempt has nothing
        to show anyway.
        """
        last_code: str | None = None
        created = False
        for revision in revisions:
            if created and revision.code == last_code:
                continue
            if not created:
                actions = [
                    {"action": "create", "file_path": file_path, "content": revision.code},
                    {"action": "create", "file_path": plan_path, "content": plan_content},
                ]
                created = True
            else:
                actions = [
                    {"action": "update", "file_path": file_path, "content": revision.code}
                ]
            self.project.commits.create(
                {
                    "branch": branch_name,
                    "commit_message": revision.message,
                    "actions": actions,
                }
            )
            last_code = revision.code


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
    trace_path: str | None = None,
) -> str:
    healer_block = ""
    if heal_attempts > 0:
        bullets = "\n".join(f"- {s}" for s in (heal_summaries or [])) or "- (no summary recorded)"
        healer_block = f"\n### Healer attempts ({heal_attempts})\n{bullets}\n"

    # The trace is a local artifact on the machine that ran the pipeline (not committed);
    # pointing at it saves the reviewer of a red MR from re-running to get a trace.
    trace_line = ""
    if trace_path:
        trace_line = f"\n**Playwright trace (local artifact on the runner):** `{trace_path}`"

    return f"""## AI-Generated Playwright Test

**Source Jira ticket:** `{key}`
**Final run status:** `{final_status or "unknown"}` · **Heal attempts:** {heal_attempts}{trace_line}

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
- [ ] Selectors are verified locators (getByTestId / getByRole / getByLabel), not raw #id/CSS
- [ ] Assertions cover the expected outcomes from the original test case
- [ ] No **real** credentials or PII (the staging dummy logins from project_context are expected)
- [ ] Test runs successfully locally
- [ ] Test is idempotent (can run multiple times without side effects)

---
*Opened by the AI test generation pipeline. The plan JSON is committed at \
`tests/generated/_plans/{key}.json` for traceability.*
"""
