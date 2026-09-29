# Locators — resilience ladder

Every locator is captured from the LIVE element and verified — never typed from memory. For each
element pick the MOST ROBUST kind it actually supports, descending only as far as you must:

1. **Stable id** → `getByTestId('login-submit')`. The runner sets `testIdAttribute: 'id'`, so an
   author-written `id` comes back from `browser_generate_locator` as `getByTestId(...)` — resolves
   to `[id="login-submit"]`, locale-independent. REJECT generated ids (`mui-component-42`, `:r0:`):
   they change every build.
2. **Accessible** → `getByRole('button', { name: 'Save', exact: true })` / `getByLabel(...)` /
   `getByText(...)`, when the element has a real role and name but no stable id.
3. **Stable CSS** → `locator('css=[name="email"]')`, anchored on a stable attribute.
4. **XPath** → `locator('xpath=//button[normalize-space()="Speichern"]')`: the legitimate LAST
   RESORT for inaccessible elements (no id, role or stable attribute). Anchor on text, an attribute
   or a structural relationship — never a brittle absolute `/html/body/div[3]/...` path.

Never skip a rung that works; never stop above one you need.

**Capturing:**
- An action's result (click, fill, navigate) shows the new Page URL but NOT the page itself; refs
  from before an action may be stale. Read the page as cheaply as you can — every snapshot stays
  in the conversation and is resent with each later request:
  - `browser_find` with a text you expect (a label, a button name) returns just the matching
    elements and their refs — use it to locate an element;
  - `browser_snapshot` with `target` (the ref of a dialog, form or list) or `depth` returns part of
    the page;
  - a full `browser_snapshot` only when you need to see the whole page, e.g. after landing on a
    new page. Never on a large external page (an article, a long list).
- ALWAYS start with `browser_generate_locator` on the element's snapshot `ref` and record what it
  returns, without the `page.` prefix. An element with an author-written id comes back as
  `getByTestId(...)` — take it. Author a CSS/XPath only when that result is on a lower rung than
  the element supports or unusable — never as your first move.
- A CSS/XPath you author is recorded only after BOTH checks: pass the RAW selector (`css=…` /
  `xpath=…`, not wrapped in `locator(...)`) as `browser_generate_locator`'s `target` (errors on 0
  matches), AND `count_matches` reports exactly 1 (generate_locator does NOT flag duplicates).
- Name-based locators (`getByRole({ name })`, `getByText`, `getByLabel`) ALWAYS carry
  `exact: true`, even if generate_locator left it out — otherwise "Add" also matches "Add admin".
  Never add `exact` to `getByTestId`, CSS or XPath.
- The #1 hallucination: `getByRole('button', { name })` for a text label. Menu items, dropdown
  options and custom controls are often `<div>`/`<span>`/`<li>`, not buttons. Open the menu and
  capture the item live.
- If you cannot verify ANY locator for an element, leave it empty and say why — NEVER guess.
