# Workflow — how one test case flows through the pipeline

> The run-time view: what happens, in what order, and which agent is called when.
> For the component/structure view, see [ARCHITECTURE.md](ARCHITECTURE.md).

The whole pipeline processes **one test case at a time** — fetched live from Jira/Xray, or read from a local raw-Xray-shaped JSON file when `TESTCASE_SOURCE=local` (the path the bundled demo uses). The Orchestrator runs this sequence: **fetch → plan → generate → run → (heal ↺) → open MR**.

## End-to-end flow

```mermaid
flowchart TD
    START([Test-case key, e.g. QA-1234 or NOTE-2]) --> FETCH

    FETCH["Fetch — Xray Client or local JSON<br/>→ ManualTestCase"] --> PLAN
    PLAN["Planner Agent + browser · gpt-oss-120b<br/>drive the flow + verify selectors on staging<br/>→ TestPlan"] --> GEN
    GEN["Generator Agent · devstral-small-2<br/>plan → Playwright code<br/>→ GeneratedTest .spec.ts"] --> RUN

    RUN["Test Runner<br/>execute .spec.ts on staging"] --> Q1{passed?}
    Q1 -- yes --> MR
    Q1 -- no --> Q2{more heal<br/>attempts left?}
    Q2 -- yes --> HEAL["Healer Agent + browser · gpt-oss-120b<br/>reproduce failure live, fix / reconcile<br/>→ HealedTest"]
    HEAL --> RUN
    Q2 -- no --> FLAG["give up healing<br/>real bug or stuck"]
    FLAG --> MR

    MR["GitLab Client<br/>branch + commit + open MR<br/>labels: ai-generated, qa-review-needed"] --> REVIEW([Human QA reviews and merges])

    classDef agent fill:#23395d,color:#fff,stroke:#0d1b2a;
    class PLAN,GEN,HEAL agent;
```

**Agents are blue.** Note the two browser-driving agents (Planner, Healer) and the single non-browser one (Generator). The loop back from Healer to Runner is the self-healing retry.

**Planner refusals short-circuit the run.** The Planner is instructed to refuse unclear or unsafe cases (forbidden routes, PII, production) by returning a plan with **no steps** and the reason in `notes`. The Orchestrator stops right there — no generation, no run, no heal attempts, no MR — and reports `status: refused` with those notes. The plan JSON is still saved for audit.

## Which agent is called when

```mermaid
sequenceDiagram
    autonumber
    participant O as Orchestrator
    participant X as Xray Client
    participant P as Planner · gpt-oss-120b
    participant B as Staging app (via MCP)
    participant G as Generator · devstral
    participant R as Test Runner
    participant H as Healer · gpt-oss-120b
    participant L as GitLab

    O->>X: fetch(issue_key)
    X-->>O: ManualTestCase
    O->>P: plan_test_case(case)
    P->>B: drive flow (fill/submit) + browser_generate_locator
    B-->>P: verified locators (id / accessible / css / xpath) + observed outcomes
    P-->>O: TestPlan
    O->>G: generate_test(plan)
    G-->>O: GeneratedTest (.spec.ts)
    O->>R: run_test(test)
    R->>B: execute .spec.ts
    R-->>O: TestRunResult

    loop while failing, up to 2 attempts
        O->>H: heal_test(test, failure, plan, case, heal history)
        H->>B: log in + reproduce failure live + browser_generate_locator
        B-->>H: correct locators + observed behavior
        H-->>O: HealedTest
        O->>R: run_test(healed)
        R-->>O: TestRunResult
    end

    O->>L: open_mr(test, plan)
    L-->>O: MR URL
```

## Stage by stage

| # | Stage | Who | In → Out | Touches | ~Time |
|---|---|---|---|---|---|
| 1 | Fetch | Xray Client *or* local JSON loader | test-case key → `ManualTestCase` | Jira/Xray API, or a local JSON file | <1s |
| 2 | Plan | **Planner** (+MCP) | case → `TestPlan` (verified selectors + page context) | drives staging in a browser (fills, submits) | 30–90s |
| 3 | Generate | **Generator** | `TestPlan` → `GeneratedTest` (guarded `test.step`s, container-scoped locators) | none (writes file) | 10–20s |
| 4 | Run | Test Runner | test → `TestRunResult` | runs the test on staging | 10–60s |
| 5 | Heal *(only if step 4 failed)* | **Healer** (+MCP) | failed test + error + plan + intent → `HealedTest`, then back to step 4 | drives staging (logs in, reproduces the failure) | 30–60s / attempt |
| 6 | Open MR *(skipped if `GITLAB_ENABLED=false`)* | GitLab Client | per-attempt test revisions + plan → MR URL | pushes branch, one commit per attempt, opens MR | <2s |

**Total: ~2–4 minutes per test case.**

## The heal loop, explained

- The Runner **never throws on a failing test** — a failure is a *healable state*, not a crash. (A genuinely hung run is caught by a hard timeout and reported as `status=error`, so the pipeline can't wedge.)
- **Compile errors never reach the Healer.** A run that produced no JSON report (`did_run=false` — the spec failed to compile/collect and never executed) goes back to the **Generator** for one regeneration with its own code + the error text. No browser is involved; only a test that actually *ran* enters the heal loop. A persistent compile error still falls through to the MR so a human sees it.
- While the test is failing, the Orchestrator calls the Healer up to **`MAX_HEAL_ATTEMPTS = 3`** times. The Healer is a **full browser agent** like the Planner: it starts with no saved session, so each attempt it logs in fresh and **reproduces the failure live** — re-performing the failing step (submitting forms, creating data, triggering the validation, even signing out / resetting a password if the failure path needs it, all within the non-prod guard) to see what the app actually does. Then it makes the smallest change that turns the test green — usually a selector/wait/URL fix, but it MAY add a step the Generator skipped (including a **recovery step** like a re-fill after a cleared field or a re-login after a sign-out) or drop one it hallucinated, to reconcile the test with its intent. It never re-plans from scratch or adds unrelated test cases.
- **The Healer reconciles against the original intent.** It also receives the `ManualTestCase` and the `TestPlan` (incl. the Planner's `notes`, verified selectors, and each step's plan-time page context — `page_url` / enclosing `container`), so it compares what the test *should* do against the failing code — staying faithful to that intent (never going green by dropping a real check) and capturing any selector it adds live (the resilience ladder id → accessible → CSS → XPath; CSS/XPath authored for the lower rungs are verified to resolve before use), never inventing one.
- **Locator-kind escalation on a stuck step.** The Orchestrator fingerprints each failure (failing test + coarse category — timeout / strict-mode / navigation / assertion — *ignoring* the specific selector) and counts consecutive recurrences. When the **same step keeps failing the same way**, re-trying the same kind of locator isn't working, so the heal message tells the Healer to **descend the resilience ladder to a different kind** — e.g. roll a persistently-failing (often hallucinated) id over to a verified `locator('xpath=…')`, exactly what a human QA engineer does for an inaccessible element. This is why the cap is 3: one attempt to confirm the failure recurs, another to escalate.
- **Each attempt sees the previous attempts' changes.** The accumulated `changes_summary` history is in the heal message with an explicit "the code already contains these changes — don't undo them" instruction, so a whole-file rewrite on attempt 2 builds on attempt 1 instead of ping-ponging back.
- **Diagnosis starts at the line the run died.** The runner extracts the failing line from the Playwright report (`error_line`), and the heal message quotes it with an explicit boundary: code after it **never executed** (don't "fix" it for this failure), code before it may have silently mis-acted — a wrong early locator usually surfaces as a *downstream* timeout. The Healer replays the test's locators in order from the top (login first) to find the first real blocker.
- **Generated tests guard each step, so failures localize.** Each plan step is wrapped in `test.step('<action>', …)` with a pre-action `expect(target, '…').toBeVisible()` before it acts and — for steps that open a modal/menu or navigate — a post-action state assert after. A missing element fails fast at the expect timeout with a labeled message instead of a 60s click timeout, and a step that fails to open a modal fails on its OWN line. The Healer reads which guard fired: a failed pre-action guard means a wrong locator *or* a prior step whose effect never landed; a failed post-action assert means this step's own trigger didn't work.
- **The Healer has a failure-mode catalog** — locator timeout, wrong URL, language mismatch, and **strict-mode violations** (`resolved N elements`), which it fixes by making the name match `exact: true` or scoping to the active dialog. It captures selectors live (`browser_generate_locator`, or a verified CSS/XPath for inaccessible elements) rather than hand-writing them.
- The Healer is told to **leave the test unchanged if the failure is a genuine app bug** rather than a selector problem — so a real regression surfaces honestly instead of being "fixed" away. This covers a **spec-vs-reality divergence**: if reproducing the flow shows the app genuinely behaves differently from what the test case demands (the case expects a disabled button, the app keeps it enabled with a validation message), the Healer keeps the assertion faithful to the test case and explains the divergence in `changes_summary` rather than weakening it to go green.
- **If it still fails after 2 attempts, the MR is opened anyway.** Healing is a convenience, not a gate — a human reviews every result regardless. The MR labels (`ai-generated`, `qa-review-needed`) and the committed plan JSON give the reviewer full context.
- **Reviewers see the heal history.** Each attempt's `changes_summary`, the heal count, and the final status are rendered into the MR description — tests that needed multiple rounds are easy to spot and scrutinize.
- **Every iteration is kept on disk, and the MR shows the full attempt chain.** The first generated spec keeps its name in `output/tests/`; the compile-retry regeneration and each heal attempt are written to their *own* sibling files — `<name>.regen.spec.ts`, `<name>.healer-attempt-1.spec.ts`, `<name>.healer-attempt-2.spec.ts` — so no iteration overwrites another and the whole heal history stays inspectable locally. The MR then commits **one commit per attempt to a single file path** under the original first-iteration filename (initial generation → optional regen → each heal; the Healer's own returned `file_name` is deliberately ignored). A reviewer opens the MR's commit view and diffs one attempt against the next — each commit's subject names the attempt (`[AI] QA-1: heal attempt 2`) and the Healer's `changes_summary` is in the commit body. An attempt that changed nothing is skipped, so there are no empty commits.
- **Run housekeeping.** At the start of every run the Orchestrator empties `output/snapshots/` (the regenerated MCP snapshot/png output, kept out of git via a `.gitkeep` + ignored contents), and it stamps the saved plan JSON with a `context_hash` (sha256 of `project_context.md` + `project_map.md`) so a plan built against stale context is auditable later.
- **Every run is logged to disk.** Each `run_one.py` invocation writes a log to `output/runs/run-<issue-key>-<timestamp>.log` (gitignored); the path is printed at the start and end of the run. The file captures **INFO by default** (`--verbose` only raises *console* verbosity), which records every pipeline step and every failure — the Planner/Healer exception text, including the gateway's error body, is logged at WARNING/ERROR. It deliberately does **not** replay the agents' conversations (the large accessibility snapshots stay in memory), so it stays small and easy to read or share when a run fails. Set `RUN_LOG_LEVEL=DEBUG` for a deeper dive. Third-party HTTP loggers (`httpx`/`openai`) are pinned to WARNING so the file stays readable and never records the gateway request headers (which carry the API key).
- **Working-memory trimming (Planner & Healer) — optional, off by default.** The browser agents *can* trim stale page snapshots from their conversation history (`SNAPSHOT_HISTORY_KEEP` enables it; milestone pages where locators were captured are anchored, transit frames stubbed, captured locators never trimmed). It ships **disabled**: live runs showed plan quality degrade with trimming on, so the full history is the default. See the trimming bullet in [ARCHITECTURE.md](ARCHITECTURE.md) and `.env.example`.
- **Vision Aid sensor (Planner & Healer) — optional, off by default.** Set `VISION_MAX_CALLS=N` (single shared knob; `PLANNER_VISION` still works as a back-compat alias) to give **both** text-only browser agents an `inspect_screen` tool: it screenshots the page and asks the **Vision Aid Agent** (`VISION_MODEL`) to describe what is actually rendered — useful when the accessibility snapshot is silent about visual state (did a dropdown open? is a modal/overlay covering the page? did a toast appear? is the button greyed out?). The budget is **per agent run**: N calls per planning run *and* N per heal attempt (each lifecycle starts fresh — not one shared pool), so a test with 3 heal attempts may use up to 3×N total, by design. It is a sensor only — the image never reaches the agent, and it never produces a selector (targeting stays on `browser_generate_locator`). Unset/`false` leaves both agents identical to before; requires a multimodal gateway model. On repeated failure each agent also self-corrects: after N consecutive `browser_generate_locator` failures (`PLANNER_LOCATOR_STEER_AFTER`, default 3) it is steered to screenshot + `inspect_screen` and re-orient instead of hammering the locator to the retry ceiling. See the vision bullet in [ARCHITECTURE.md](ARCHITECTURE.md) and `.env.example`. (A complex failure-path repro plus vision adds turns — if a run reports `UsageLimitExceeded` mid-heal, raise `AGENT_REQUEST_LIMIT`.)
- **GitLab is optional.** With `GITLAB_ENABLED=false` (e.g. a local Docker run) the pipeline stops after the run/heal loop and leaves the test + plan in `output/` — no branch, no MR (default is `true`, so a normal run still opens one). The direct-connect proxy policy covers the Xray + GitLab `requests` clients too, so the container reaches them without an env proxy.

## What triggers a run

```mermaid
flowchart LR
    subgraph now[" Manual "]
        CLI["uv run python -m<br/>ai_test_gen.orchestrator QA-1234"] --> ORCH1[Orchestrator]
    end
    subgraph later[" CI — possible extension "]
        JIRA["Jira: test case marked<br/>'Ready for Automation'"] --> HOOK[webhook] --> CI["CI job<br/>Docker container"] --> ORCH2[Orchestrator]
    end
```

- **Manual:** run it by hand, one Jira key at a time. Each agent and the generated test log in live from the `project_context.md` dummy creds (context-driven auth — no saved session); any data a test *creates* is randomized at run time, so the same test can be replayed in regression without colliding. The same container image runs **standalone for local generation** (`docker compose run --rm pipeline QA-1234`), with `GITLAB_ENABLED=false` to skip the MR.
- **CI (a possible extension, not yet built):** a Jira status change webhooks a CI job, which runs the same Orchestrator inside a locked-down container — one job per test case, fanned out for batches.

## How this grows (planned)

- **Translator (4th agent).** For migrating the existing Selenium suite: `Selenium test → Translator (+MCP) → Playwright test → Runner → (Healer) → MR`. Same pipeline shape, different front door.
- **RAG-assisted Generator.** Once enough Playwright tests exist in the repo, the Generator first retrieves 2–3 similar existing tests (Qdrant vector search → cross-encoder rerank) and injects them as examples — "write something that looks like these" markedly improves output. This is the only change to the core loop; everything else stays the same.
