# Project Context — Demo Notes app

## 1. What the app is
- A simple notes app: you create an account (or use the demo account), log in, and keep a
  personal list of notes that you can add, edit, and delete.
- Things tests work with: a user account (email + password) and a note (a title and an
  optional body). Each user only sees their own notes.
- It runs locally at http://localhost:3000 and only ever holds throwaway demo data.

## 2. Authentication model
- No saved session is used: every test logs in from scratch at the start of the run.
- Login is a normal email + password form inside the app — no external login page.
- There is only one kind of user. Unless the test is about registering a new account, log in
  as the demo user from §3.
- To switch users mid-test: click Log out, then log in (or register) as the other user.
- Generated tests use the demo credentials in §3 as plain values. Never put real credentials
  in a test.

## 3. Test users (always available)
The demo account is always there, even in a brand-new browser, so logging in with it always works.

| Role          | Email          | Password    | What this user can do                  |
| ------------- | -------------- | ----------- | -------------------------------------- |
| Standard user | demo@demo.test | Passw0rd!   | Log in and create/edit/delete own notes |

## 4. Registration & test-data conventions
- Registration creates a new account — don't use the demo account for a registration test.
- Unique email every run (required): the app refuses an email that is already registered, so
  add a timestamp or short random token to the address, e.g. `qa-20260622-143200@demo.test`.
  Generate it when the test runs so reruns never clash.
- Password: any value works as long as "Password" and "Confirm password" match
  (e.g. `NewPass123!`).
- A note needs a title; the body can stay empty.
- Every test starts in a fresh browser with no notes and no extra accounts. If a test needs a
  note (e.g. to edit or delete it), the test creates it first.

## 5. Selector rules (standard)
- Do NOT list selectors here or in project_map.md. The agents capture every locator LIVE and pick
  the most robust kind the element supports — the resilience ladder: id (`getByTestId`) >
  accessible (`getByRole`/`getByLabel`/`getByText`) > CSS (`locator('css=…')`) > XPath
  (`locator('xpath=…')`). An id is not "better" than an XPath when the element has no id.
- Inaccessible elements (no id, no usable role/name) get a verified CSS or XPath — that is the
  correct fix, not a hack. Capture with Playwright MCP and confirm a locator resolves to the
  intended element before recording it; never invent one.

## 6. Localization
- English only.

## 7. Behavior guardrails
- Local demo only (http://localhost:3000); there is no production environment.
- Logging out ends the session. Only log out when the test needs it (testing logout, switching
  users), and log back in before any later step that needs you logged in.
- Stay within the test's scope: only change or delete notes the test itself created.
