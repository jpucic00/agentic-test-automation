# Project Context — Demo Notes app

## 1. What the app is
- A tiny notes app used as the application under test for this pipeline's demo. Users
  register, log in, and create / edit / delete their own notes.
- There is no backend or database: accounts, the session, and notes are all stored in the
  browser's `localStorage`.
- Core entities: a User (email + password) and a Note (title + body) owned by the logged-in user.
- It runs locally at http://localhost:3000 and holds only disposable demo data.

## 2. Authentication model
- Mock auth — no real identity provider (no Keycloak / OAuth). Login and registration are
  plain in-app forms.
- Opening the base URL redirects to `/login`; submit the form to sign in. There is no
  external redirect.
- At the start of a scenario, sign in as the user the test needs. This app has a single user
  role; unless the test registers a new account, log in as the seeded demo user (see §3).
- Generated tests sign in directly with the seeded demo credentials below, embedded as
  literals — they are disposable, non-production values. Never put real credentials in a test.
- To switch identity: click Log out in the navbar, then log in or register as the other user.

## 3. Test users (seeded — re-created on every page load)
The app re-seeds this account into `localStorage` on every page load, so it is always
available even though each test run starts with an empty browser.

| Role          | Email          | Password    | What this user can do                              |
| ------------- | -------------- | ----------- | -------------------------------------------------- |
| Standard user | demo@demo.test | Passw0rd!   | Register / log in and create/edit/delete own notes |

## 4. Registration & test-data conventions
- Registration CREATES a new account; do not reuse the seeded demo user for a registration test.
- Uniqueness (required): the app rejects an email that already exists, so every registration
  must use a UNIQUE email per run — append a timestamp or short random token, for example
  `qa-20260622-143200@demo.test`. Compute the suffix in the test at run time so reruns do not
  collide.
- Password: the form only requires the password and its confirmation to match; any non-empty
  value works (for example `NewPass123!`).
- A note needs a title; the body is optional.
<!-- The registration flow's entry point, steps, and fields live in project_map.md, not here. -->

## 5. Selector rules (capture live, never hallucinate)
- Do NOT list selectors here or in project_map.md. The agents capture every locator LIVE from
  the running app and pick the most robust kind the element supports — the resilience ladder:
  id (`getByTestId`) > accessible (`getByRole`/`getByLabel`/`getByText`) > CSS > XPath. (This demo
  app is a mixed-accessibility fixture ON PURPOSE: only the login page is fully id'd; register/notes
  inputs are label-only (`getByLabel`, no id) and the New-note/Save/Cancel/Edit/Delete/Log-out
  controls plus the delete dialog are non-semantic `<div>`s with no role/id — those resolve to a
  verified CSS/XPath/text locator. So expect the full ladder, not `getByTestId` everywhere.)
- Capture with Playwright MCP (`browser_generate_locator`, plus the `browser_verify_*` tools to
  confirm an authored CSS/XPath). Record only a locator you verified resolves; never invent one.
- Per-note controls are generated per row at run time. Locate a specific note by its visible
  title, then act on the edit/delete control in that same row — don't rely on a fixed per-note id.

## 6. Localization
- English only; visible text is stable. The agents still prefer locale-independent locators.

## 7. Behavior guardrails
- Local demo only (http://localhost:3000). There is no production environment.
- Each run starts with a FRESH, empty `localStorage`. The seeded demo user is re-created on
  every page load (see §3); any notes or extra accounts a scenario needs must be created within
  that scenario.
- Session-invalidating action: clicking Log out ends the current session — only do it if the
  test is about logging out, and keep it as the final step.
- Stay within the test's scope: do not clear `localStorage` or delete notes the scenario did not
  create.
