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
- There is no `/login` URL: click the **Login** control in the nav (`#metaMenuItem5`),
  which redirects to **Keycloak** (`#username`, `#password`, submit `#kc-login`), then
  returns to the app. (Full step-by-step lives in project_map.md.)
- **At the start of a scenario, log in as the role the test requires** (see §3). If the
  test names no role, use **<DEFAULT ROLE>**.
- To change role, log out (<logout selector/route>) and log in as the other user.
- **Generated tests must read credentials from `process.env.<VAR>`** (the env-var column
  in §3) — NEVER hardcode passwords in a `.spec.ts`.

## 3. Test users (staging dummies — pre-created)
<!-- Use EMAIL/PASSWORD to log in live during planning/healing. The ENV VAR names are
     what generated .spec.ts files read via process.env. Add the same vars to .env. -->

| Role | Email / username | Password | Env vars (generated tests) | What this role can do |
|------|------------------|----------|----------------------------|-----------------------|
| <Admin>   | <admin@stage.example>   | <password> | ADMIN_EMAIL / ADMIN_PASSWORD     | <full org admin: manage users, billing…> |
| <Manager> | <manager@stage.example> | <password> | MANAGER_EMAIL / MANAGER_PASSWORD | <…> |
| <Member>  | <member@stage.example>  | <password> | MEMBER_EMAIL / MEMBER_PASSWORD   | <…> |

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

## 6. Selector rules (never hallucinate)
- Prefer stable `id` selectors (`#save-button`) — this app's team writes IDs manually.
- Fall back to ARIA role + accessible name only when no id exists.
- Use ONLY selectors observed in the live accessibility snapshot. If you can't verify a
  selector by navigating, leave it empty and say so — never invent one. No hashed CSS
  classes, no XPath.

## 7. Localization (EN / DE)
- The UI renders English or German by locale; visible labels may be German.
- Prefer locale-independent selectors (IDs/roles). If you must match text, use it exactly
  as it appears in the snapshot.

## 8. Behavior guardrails
- Staging only; never act on production.
- Stay within the test's scope — don't delete other users' data or change billing/settings
  unless the test is about that.
- Don't switch UI language/locale unless the test requires it.
- <project-specific guardrails>
