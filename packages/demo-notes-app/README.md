# Demo Notes app

A tiny [Next.js](https://nextjs.org/) app — register, log in, and manage notes — used as the
**application under test** when demonstrating the AI test-generation pipeline in this repo. It has
no backend and no database: users, the session, and notes are all stored in the browser's
`localStorage`.

It exists so you can run the whole pipeline (plan → generate → run → heal) against a **real, local
app** without needing a Jira/Xray tenant or a private staging environment.

## Run it

```bash
cd packages/demo-notes-app
npm install
npm run dev          # serves http://localhost:3000
```

`npm run build` then `npm start` runs the production build instead.

## Seeded account

A demo user is **re-seeded into `localStorage` on every page load**, so the "log in" scenarios are
deterministic even though each automated test run starts with a fresh, empty browser:

| Email            | Password    |
| ---------------- | ----------- |
| `demo@demo.test` | `Passw0rd!` |

New registrations should use a **unique email per run** (the app rejects duplicates) — the pipeline's
test cases append a timestamp/random suffix for exactly this reason.

## Routes

| Route       | Purpose                          | Auth                        |
| ----------- | -------------------------------- | --------------------------- |
| `/`         | redirects to `/login`            | —                           |
| `/login`    | log in                           | public                      |
| `/register` | create an account                | public                      |
| `/notes`    | list / create / edit / delete    | redirects to `/login` if logged out |

## Accessibility profile (intentionally degraded)

This app is a **mixed-accessibility fixture** on purpose: it exercises the pipeline's locator
**resilience ladder** (`getByTestId` → `getByRole`/`getByLabel`/`getByText` → CSS → XPath) instead
of letting every element resolve to an `id`. The pipeline is configured with Playwright's
`testIdAttribute: "id"`, so where ids survive they surface as `getByTestId('...')` — but most
controls deliberately don't survive. By surface:

- **Login page — fully accessible (the only intact surface).** Every field/button keeps its `id`
  and semantics: `login-email`, `login-password`, `login-submit`, `login-error`, plus the
  `nav-login` / `nav-register` links. This is every scenario's entry point and the clean control.
- **Register page — label-only inputs + a non-semantic submit.** The email/password/confirm inputs
  have **no `id`**; they're associated only by an implicit wrapping `<label>` → reachable by
  `getByLabel`, not `getByTestId`. "Register" is a `<div class="btn">` (no `<button>`/role/`id`) and
  the error has no `id`/`role`.
- **Notes page — non-semantic action controls, id-less rows.** "New note", "Save note", "Cancel",
  each row's "Edit"/"Delete", and the delete-confirm dialog's "Delete"/"Cancel" are all
  `<div class="btn">` (no role/`id`/aria). The editor's title/body inputs are label-only (like
  register). Notes render **without per-row ids** — locate a note by its visible title, then act on
  the Edit/Delete control in that same row. The delete dialog has **no** `role="dialog"`/aria/`id`
  (scope to its `.modal` container, then by text).
- **Navbar (logged in) — non-semantic logout.** "Log out" is a `<div class="btn">` (no
  `<button>`/role/`id`); the user-email indicator is a plain `<span>` with no `id` (match its text).

The `<div class="btn">` controls *look and behave* like buttons (styled identically, real
`onClick`) but expose **no** role/label/`id` to assistive tech or to `getByRole` — so the agents
must capture a verified CSS/XPath/text locator instead. `project_map.md` / `project_context.md`
deliberately list **no selectors**; the agents capture every locator live from the running app.

## Point the pipeline at it

From the repo root, with the app running on port 3000, use the demo profile (see the repo
[`.env.example`](../../.env.example) "Demo profile" block):

```bash
TESTCASE_SOURCE=local
LOCAL_TESTCASE_DIR=packages/demo-notes-app/test-cases
PROJECT_CONTEXT_PATH=packages/demo-notes-app/project_context.md
PROJECT_MAP_PATH=packages/demo-notes-app/project_map.md
STAGING_BASE_URL=http://localhost:3000
GITLAB_ENABLED=false
```

Then run a bundled test case (no Jira, no GitLab):

```bash
uv run python scripts/run_one.py NOTE-2 --verbose
```

The test cases live in [`test-cases/`](test-cases/) as raw-Xray-shaped JSON — see
[`project_context.md`](project_context.md) for conventions.

## Legacy suite (KB-seeding demo corpus)

[`legacy-suite/`](legacy-suite/) is a miniature "existing test repository" for trying the
knowledge-base seeding workflow (`scripts/seed_kb.py`) without a real corpus: two Selenium/Java
tests annotated `@Xray(testCase = "NOTE-…")` in a realistic suite layout (`main` page-object
packages holding the `By.*` locators, a shared `core` package, one deliberately unresolvable
call), plus one hand-written Playwright spec. The Java files are static fixtures — they are never
compiled or executed (IDE import errors there are expected), and `tsconfig.json` excludes the
directory from the demo app's build. See SETUP.md §7.2 for the dry-run seeding walkthrough.
