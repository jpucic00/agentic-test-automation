<!--
TEMPLATE — copy to `project_map.md` (gitignored) and fill in.

`project_map.md` is injected into the Planner and Healer ONLY (the agents that drive
the browser) — NOT the Generator. It is a SITEMAP: routes, flows, roles, and quirks,
so the agents navigate efficiently and know where things are.

DO NOT list element ids / CSS / XPath / selectors here. The Planner and Healer capture
every locator LIVE from the running app and pick the most robust one the element
supports along the resilience ladder — id > accessible (role/label/text) > CSS > XPath.
You do not need to (and should not) maintain a selector catalog for findability; describe
WHERE things are and WHAT the flow is, in plain words, and let the agents find the
elements. Naming a control by its visible label or purpose ("the Login button in the top
nav") is fine; pasting `#ids` is not.

HOW TO FILL: walk staging with the headed browser (`PLAYWRIGHT_MCP_HEADED=1`) and write
down the routes and the order of actions in each flow. Start small — the auth flow + the
2–3 flows your first test cases need — and grow it whenever a generated test fails for a
reason a human would have caught. Delete the guidance comments.
-->

# Application Map — <APP NAME>

## Base
- Staging base URL: <https://staging.example.com> (non-production)
- Landing after login: <route>
- Language: <EN default / DE available> — language toggle: <where it is / N/A>

## Navigation (top bar / sidebar)
| Element (by label/purpose) | Goes to | Visible to |
|----------------------------|---------|------------|
| Login | login flow (see below) | unauthenticated |
| <Dashboard> | <route> | <all roles> |
| <Admin> | <route> | <Admin only> |
| <User menu / logout> | <…> | <authenticated> |

## Auth flow (login) — step by step
<!-- Describe the SEQUENCE; the agent finds each control live. -->
1. Go to the base URL → landing page.
2. Click the **Login** control in the nav → redirects to <e.g. Keycloak> login.
3. Enter the email and password, then submit.
4. Lands on <route>. Logged-in indicator: <what tells you you're in, e.g. the user-menu shows the email>.
5. Logout: <where the logout control lives / route>.

## Registration flows — step by step
<!-- For "create org/user first" scenarios. Describe fields + order, not selectors. -->
### Organization signup
- Entry: <route or which button opens it>
- Steps: 1) <field>  2) <field>  3) submit
- Success indicator: <what confirms it / redirect route>
### Create / invite user (as Admin)
- Entry: <route or which control opens it>
- Steps + fields: <…>

## Routes & access by role
| Route | Purpose | Auth | Roles |
|-------|---------|------|-------|
| <`/dashboard`> | <overview> | yes | all |
| <`/admin/users`> | <user management> | yes | Admin |
| <`/projects`> | <…> | yes | <…> |

## Key features (the flows your test cases exercise)
<!-- One block per page/feature your tests touch. Describe the controls by name/purpose. -->
### Feature: <User management>
- Route: <…>; roles: <Admin>
- Controls: <create-user button, a row per user, delete + confirm in a modal>
- Notes: <delete shows a modal; list paginates at 25>
### Feature: <…>
- Route / roles / controls / notes: <…>

## Known quirks / flakiness
- <e.g. dashboard loads async — wait for the page to settle before asserting>
- <modals animate; the element must be stable before clicking>
- <EN/DE: labels differ by locale — the agents prefer locale-independent locators, but call
  out anything text-dependent here>
- <any element that is hard to reach / inaccessible (no id, no role) — note it so the agent
  knows to capture a CSS/XPath locator for it rather than expecting an id>
