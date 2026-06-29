<!--
TEMPLATE — copy to `project_context.md` (gitignored) and fill it in.

`project_context.md` is injected into the system prompt of EVERY agent (Planner, Generator,
Healer). It holds the "who / what / how": conventions, roles, test users/credentials, and
test-data rules — NOT the page-by-page map (that's project_map.md).

HOW TO FILL THIS IN:
- Each bullet written as `<a question?>` is a PROMPT — replace the whole `<…>` with your answer
  (delete the brackets and the question text; leave your answer).
- Fill every TABLE by copying its `<…>` example row once per real entry, then delete the
  example row.
- Sections labelled "(standard — keep as written)" are pipeline policy: leave them unchanged.
- Delete every `<!-- … -->` guidance comment, and any section you don't need, when done.
- NEVER put selectors / element ids / CSS / XPath here — the agents capture locators live from
  the running app. Describe behaviour, roles, data, and conventions only.
- SECURITY: only STAGING / dummy credentials. This text is sent to the LLM gateway; the filled
  file is gitignored and never committed.
- Keep it tight — mid-tier models degrade past ~30K tokens. Fill what your first test cases need
  and grow it over time.
-->

# Project Context — <APP NAME>

## 1. What the app is
<!-- 2–4 sentences that frame the app for someone who has never seen it. -->
- <What does the product do, and who uses it?>
- <What are the core entities — the nouns your tests create or act on (e.g. Organization, User, Project)?>
- <Anything else a tester must know up front (multi-tenant? key workflow? important constraints)?>

## 2. Authentication model
<!-- WHAT GOES HERE: the auth MODEL only — kind of login, default role, how credentials are used.
     The click-by-click login STEPS live in project_map.md ("Auth flow"); don't repeat them here.
     The pipeline uses NO saved session — each scenario logs in live as the role it needs. -->
- No saved session is used: every test authenticates from scratch at the start of the run.
- <What KIND of login is it? (e.g. an in-app form, or a redirect to Keycloak/OAuth and back) — one line; the step-by-step lives in project_map.md.>
- <Which role should a test use when it names no role (the DEFAULT role)?>
- <How do you switch identity mid-scenario (e.g. log out, then log in as another user)?>
- Generated tests sign in with the dummy credentials in §3, embedded as literals. Never put
  real/production credentials, tokens, or PII in a test.

## 3. Test users (staging dummies — pre-created)
<!-- Used both for live login during planning/healing AND as literals in the generated test.
     One row per role. Copy the example row per real user, then delete the example row. -->
| Role | Email / username | Password | What this role can do |
|------|------------------|----------|-----------------------|
| <role> | <email> | <password> | <permissions / what tests use this role for> |

## 4. Pre-existing data (optional)
<!-- Orgs / accounts / records the §3 users already own, for tests that do NOT create their own.
     Delete this whole section if your tests always create their own data. -->
| Name | Type / plan | Owned by | Seeded data / notes |
|------|-------------|----------|---------------------|
| <name> | <type> | <which role owns it> | <what's already in it> |

## 5. Registration & test-data conventions
<!-- WHAT GOES HERE: the RULES for data a test creates (uniqueness, formats, policies) — the
     Generator needs these and can't browse. The entry point, steps, and field list of each
     creation flow live in project_map.md ("Registration / data-creation flows"); don't repeat them
     here. Delete this section if no test creates data. -->
- <Which kinds of records do tests create (e.g. a new org, a new user)?>
- Uniqueness (required): every created record must be unique per run. <Give the suffix format to
  append (e.g. a timestamp or short random token) so reruns don't collide on "already exists".>
- <What email pattern does signup accept (e.g. qa+<unique>@example.com)?>
- <What password policy must a generated value satisfy (length, character classes)?>

## 6. Selector rules (standard — keep as written)
- Do NOT list selectors here or in project_map.md. The agents capture every locator LIVE and pick
  the most robust kind the element supports — the resilience ladder: id (`getByTestId`) >
  accessible (`getByRole`/`getByLabel`/`getByText`) > CSS (`locator('css=…')`) > XPath
  (`locator('xpath=…')`). An id is not "better" than an XPath when the element has no id.
- Inaccessible elements (no id, no usable role/name) get a verified CSS or XPath — that is the
  correct fix, not a hack. Capture with Playwright MCP and confirm a locator resolves to the
  intended element before recording it; never invent one.

## 7. Localization
<!-- Delete this section if the app is single-language. -->
- <Which languages does the UI render, and how is the language chosen (locale, toggle)?>
- <Is there any text a test depends on that differs by language? (the agents prefer locale-independent locators)>

## 8. Behavior guardrails
<!-- Keep the staging-only + session-killer rules; add your app's specifics. -->
- Staging / test environment only; never act on production.
- Stay within the test's scope — don't delete other users' data or change global settings unless
  the test is specifically about that.
- Session-invalidating actions end the current live login mid-scenario (signing out, "sign out of
  all devices", changing or resetting a password, <your app's equivalents>): never trigger them
  while exploring; if a test requires one, it must be the test's final steps.
- <Any other project-specific guardrail (don't toggle language, don't touch billing, etc.)?>
