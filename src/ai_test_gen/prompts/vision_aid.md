# Seeing the page (vision)

`inspect_screen("…")` captures the CURRENT page and asks the **Vision Aid Agent** (a vision model)
to describe it. Use it when the accessibility snapshot is ambiguous or silent about visual state.

**Check right after every state-changing action**, with a narrow question:
- opened a dropdown or dialog → did it open, and does it show what you expect?
- closed one → is the page usable again?
- submitted a form → success (message / navigation) or a validation error? Is the button you
  expected disabled actually disabled?

Make each check its OWN turn: call `inspect_screen`, read the answer, then act. Never combine it
with a click or navigation in the same turn — the screenshot could show the page you are moving
to instead.

Every answer has two parts: `Answer:` (flags a question whose premise doesn't match the page) and
`On screen:` (what is actually rendered). ALWAYS read `On screen:` — if it contradicts where you
think you are, RE-ORIENT first (close the overlay, navigate back, log in again). You never need a
separate "what page am I on?" call — every answer already tells you.

Vision reads pixels only: it NEVER returns a selector. Never ask it for an `id`, a `data-testid`, a
CSS/XPath selector, a locator, or the HTML, tags or attributes of an element — capture those with
`browser_generate_locator`. Calls count against a per-run budget; spend them on the checkpoints
above, not on idle looks or selector hunts.
