<!--
TEMPLATE — copy to `project_map.md` (gitignored) and fill it in.

`project_map.md` is injected into the Planner and Healer ONLY (the browser-driving agents),
NOT the Generator. It is a SITEMAP: routes, flows, roles, and quirks.

HOW TO FILL THIS IN:
- Each bullet written as `<a question?>` is a PROMPT — replace the whole `<…>` with your answer.
- Fill every TABLE by copying its `<…>` example row once per real entry, then delete the
  example row.
- Delete every `<!-- … -->` guidance comment, and any section you don't need, when done.
- DO NOT list element ids / CSS / XPath / selectors. The agents capture locators LIVE and climb
  the resilience ladder (id > accessible > CSS > XPath). Describe WHERE things are and WHAT each
  flow is, in plain words — naming a control by its visible label or purpose ("the Login button
  in the top nav") is fine; pasting `#ids` is not.
- Start small — the auth flow plus the 2–3 flows your first test cases need — and grow it
  whenever a generated test fails for a reason a human would have caught.
-->

# Application Map — <APP NAME>

## Base
- <What is the staging base URL (non-production)?>
- <What route do you land on after a successful login?>
- <Which languages does it render, and where is the language toggle (or "single language")?>

## Navigation (top bar / sidebar)
<!-- The persistent nav. One row per item, described by label/purpose — not by selector.
     Copy the example row per real item, then delete the example row. -->
| Element (by label/purpose) | Goes to / does | Visible to |
|----------------------------|----------------|------------|
| <e.g. Login> | <where it leads / what it does> | <who sees it: everyone / logged-in / Admin> |

## Auth flow (login) — step by step
<!-- WHAT GOES HERE: the click-by-click login SEQUENCE the agent replays. The auth MODEL (no saved
     session, default role, credentials) lives in project_context.md §2 — not here. Describe actions
     in order; the agent finds each control live, so no selectors. -->
1. <First action (e.g. open the base URL → it redirects to the login page)>
2. <Next action (e.g. enter email and password, then submit)>
3. <What confirms success, and where do you land?>
4. <What happens on a failed login?>
5. <How do you log out?>

## Registration / data-creation flows — step by step
<!-- WHAT GOES HERE: the entry point, ordered steps, and FIELD NAMES the agent fills. The data RULES
     (uniqueness, email/password format) live in project_context.md §5 — not here. One block per
     flow; delete the section if none. -->
### <Flow name, e.g. Organization signup>
- <Entry point — a route, or which control opens it?>
- <The ordered steps and the fields to fill?>
- <What confirms success (redirect / message)?>

## Directly-addressable routes (optional)
<!-- ONLY list URLs the app lets you navigate to DIRECTLY (deep-linkable — you could paste them in
     the address bar and land there). The agent may browser_navigate to these. If a feature is
     reached by CLICKING through the UI (a menu/tab, no typed URL) — common in SPAs — do NOT invent a
     route for it here; describe the click path under "Key features" instead. When unsure, leave it
     out: the agent navigates like a user by default. Delete this section if the app isn't
     deep-linkable. One row per addressable route. -->
| Route | Purpose | Auth | Roles |
|-------|---------|------|-------|
| <`/route`> | <what it's for> | <public / logged-in> | <which roles can reach it> |

## Key features (the flows your test cases exercise)
<!-- One block per page/feature your tests touch. Describe controls by name/purpose. -->
### Feature: <name>
- <Which route is it on, and which roles can reach it?>
- <What are the key controls and what do they do (e.g. a create button, one row per item, a delete + confirm dialog)?>
- <Anything stateful: modals, async loading, pagination?>

## Known quirks / flakiness
<!-- Anything that has bitten (or will bite) a generated test. Delete if none yet. -->
- <Async loads or animations a test must wait for?>
- <Any elements that are hard to reach or inaccessible (no id, no role) — flag them so the agent expects to capture a CSS/XPath locator?>
- <Locale-dependent text, run-time-generated ids, or anything that only appears after an action?>
