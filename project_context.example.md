<!--
TEMPLATE — copy to `project_context.md` (gitignored) and fill in.

`project_context.md` is injected into the system prompt of EVERY agent (Planner,
Generator, Healer). Put CONVENTIONS, ROLES, TEST USERS/CREDENTIALS, and TEST-DATA
rules here — the "who/what/how", not the page-by-page map (that's project_map.md).

SECURITY: only ever put STAGING dummy credentials here. This text is sent to the
LLM gateway, and the filled file is gitignored so it is never committed.

SIZE: keep it tight. Mid-tier models degrade past ~30K tokens — fill what your
first test cases need and grow it over time. Delete the guidance comments.
-->

# Project Context — <APP NAME>

## 1. What the app is
<!-- 2–4 sentences. Domain, core entities, and what testers care about. -->
- Product: <e.g. a multi-tenant SaaS where an Organization owns Users with Roles>
- Core entities: <Organization, User, Role, Project, …>
- Staging is non-production; all data here is disposable/test data.

## 2. Authentication model
<!-- No saved session is used. Each scenario authenticates as the role it needs. -->
- There is no `/login` URL: click the **Login** control in the nav, which redirects to
  <e.g. Keycloak> for the email/password, then returns to the app. (Full step-by-step lives
  in project_map.md — described in words, no selectors.)
- **At the start of a scenario, log in as the role the test requires** (see §3). If the
  test names no role, use **<DEFAULT ROLE>**.
- To change role, log out and log in as the other user.
- **Generated tests use these dummy staging logins directly** (the email/password in §3) —
  they are disposable non-prod credentials embedded as literals. Never put real/production
  credentials, tokens, or PII in a `.spec.ts`.

## 3. Test users (staging dummies — pre-created)
<!-- Use EMAIL/PASSWORD both for live login during planning/healing AND directly (as
     literals) in the generated .spec.ts. These are disposable staging dummies. -->

| Role | Email / username | Password | What this role can do |
|------|------------------|----------|-----------------------|
| <Admin>   | <admin@stage.example>   | <password> | <full org admin: manage users, billing…> |
| <Manager> | <manager@stage.example> | <password> | <…> |
| <Member>  | <member@stage.example>  | <password> | <…> |

## 4. Dummy organizations (pre-existing)
<!-- Orgs the §3 users already belong to, for tests that do NOT register a new org. -->

| Org name | Plan / type | Owner role | Seeded data / notes |
|----------|-------------|------------|---------------------|
| <Acme QA Org> | <Pro> | <Admin> | <has projects X, Y; 3 members> |

## 5. Registration & test-data conventions
<!-- For scenarios whose first steps are "register an org" / "create a user". The agent
     CREATES NEW data here — generate it, don't reuse the §3 dummies. -->
- **Uniqueness (required):** every created org/user must be unique per run — append a
  unique suffix, e.g. `QA Org {YYYYMMDD-HHMMSS}` or a short random token, so reruns
  don't collide on "already exists".
- **Email pattern:** <e.g. qa+<unique>@example.com — a domain staging accepts for signup>.
- **Password policy:** <e.g. ≥12 chars, 1 upper, 1 digit, 1 symbol — generate one that passes>.
- **Registration types & required fields:**
  - *Organization signup* — fields: <org name, admin email, password, plan, …>
  - *Create / invite user (as Admin)* — fields: <email, role, …>
  - <other signup/creation flows>
- After creating an identity, continue the scenario as that new identity.

## 6. Selector rules (capture live, never hallucinate)
- Do NOT list selectors in this file or in project_map.md. The agents capture every locator
  LIVE from the running app and pick the most robust kind the element supports — the resilience
  ladder: id (`getByTestId`) > accessible (`getByRole`/`getByLabel`/`getByText`) > CSS
  (`locator('css=…')`) > XPath (`locator('xpath=…')`). An id is not "better" than an XPath when
  the element has no id; the best locator is the highest rung the element actually supports.
- This works on ANY app — fully accessible or barely accessible. Inaccessible elements (no id,
  no usable role/name) get a verified CSS or XPath; that is the correct fix, not a fallback hack.
- Capture with Playwright MCP (`browser_generate_locator`, which also accepts a unique CSS/XPath
  as its target so an authored one can be verified; plus the `browser_verify_*` tools). Record
  ONLY a locator you verified resolves to the intended element. If you can't verify one, leave it
  empty and say so — never invent one from memory.

## 7. Localization (EN / DE)
- The UI renders English or German by locale; visible labels may be German.
- The agents prefer locale-independent locators (an `id` → `getByTestId`, or a stable CSS/XPath).
  If a locator must match text, use it exactly as it appears in the snapshot.

## 8. Behavior guardrails
- Staging only; never act on production.
- Stay within the test's scope — don't delete other users' data or change billing/settings
  unless the test is about that.
- Don't switch UI language/locale unless the test requires it.
- **Session-invalidating actions** — these kill the CURRENT live login mid-scenario: signing
  out, "sign out of all devices", changing or resetting a password, <your app's equivalents>.
  Never trigger them while exploring; if a test requires one, it must be the test's final steps.
- <project-specific guardrails>
