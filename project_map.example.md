<!--
TEMPLATE — copy to `project_map.md` (gitignored) and fill in.

`project_map.md` is injected into the Planner and Healer ONLY (the agents that drive
the browser) — NOT the Generator. It is a SITEMAP: routes, flows, and the REAL
element `id`s on each page, so the agents navigate efficiently and pick correct
locators instead of guessing.

SELECTOR SHORTHAND: `#foo` below means "an element with `id="foo"`". The agents don't
copy it verbatim — they verify each element live with `browser_generate_locator` and
record `getByTestId('foo')` (the MCP maps the `id` attribute to the test id). Elements
with no id fall back to `getByRole`/`getByLabel`.

HOW TO FILL: harvest real values from staging with the headed browser
(`PLAYWRIGHT_MCP_HEADED=1`) — open each page and read the element `id`s (or call
`browser_generate_locator`). Start small — auth flow + the 2–3 flows your first test
cases need — and grow it whenever a generated test fails for a reason a human would
have caught. Delete the guidance comments.
-->

# Application Map — <APP NAME>

## Base
- Staging base URL: <https://staging.example.com> (non-production)
- Landing after login: <route>
- Language: <EN default / DE available> — toggle: <selector or N/A>

## Navigation (top bar / sidebar)
| Element | Selector | Goes to | Visible to |
|---------|----------|---------|------------|
| Login | `#metaMenuItem5` | Keycloak login | unauthenticated |
| <Dashboard> | <#…> | <route> | <all roles> |
| <Admin> | <#…> | <route> | <Admin only> |
| <User menu / logout> | <#…> | <…> | <authenticated> |

## Auth flow (login) — step by step
1. Go to the base URL → landing page.
2. Click `#metaMenuItem5` → redirects to Keycloak.
3. Fill `#username` and `#password`; click `#kc-login`.
4. Lands on <route>. Logged-in indicator: <selector, e.g. `#user-menu`>.
5. Logout: <selector / route>.

## Registration flows — step by step
<!-- For "create org/user first" scenarios. Real selectors. -->
### Organization signup
- Entry: <route or button selector>
- Steps: 1) <field> (`#…`)  2) <field> (`#…`)  3) submit (`#…`)
- Success indicator: <selector / redirect route>
### Create / invite user (as Admin)
- Entry: <route or `#…`>
- Steps + selectors: <…>

## Routes & access by role
| Route | Purpose | Auth | Roles | Key selectors |
|-------|---------|------|-------|---------------|
| <`/dashboard`> | <overview> | yes | all | <`#…`> |
| <`/admin/users`> | <user management> | yes | Admin | <table `#users-table`, add `#add-user`> |
| <`/projects`> | <…> | yes | <…> | <…> |

## Key features (the flows your test cases exercise)
<!-- One block per page/feature your tests touch, with the selectors that matter. -->
### Feature: <User management>
- Route: <…>; roles: <Admin>
- Elements: <create `#add-user`, row `#user-row-<id>`, delete `#delete-user`, confirm `#confirm-delete`>
- Notes: <delete shows a modal; list paginates at 25>
### Feature: <…>
- Route / roles / elements / notes: <…>

## Known quirks / flakiness
- <e.g. dashboard loads async — wait for `#dashboard-ready` before asserting>
- <modals animate; the element must be stable before clicking>
- <EN/DE: labels differ by locale; rely on IDs, not visible text>
