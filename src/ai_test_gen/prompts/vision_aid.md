# Seeing the page (vision)

You have an `inspect_screen` tool: it captures the CURRENT page and shows that screenshot to the
**Vision Aid Agent** (a vision-capable model), which returns a short text description — so you can
confirm what actually happened on screen when the accessibility snapshot is ambiguous or silent
about visual state. (The Planner and the Healer share this same tool.)

**Run a vision check right after every state-changing action, to confirm the new state before you
continue:**
- after opening a dropdown or modal/dialog/drawer → did it actually open, and does it show what you
  expect?
- after closing a dropdown or modal/dialog → did it actually close (is the page usable again)?
- after submitting a form → did it succeed (success message / navigation), or is a validation error
  shown? is the button you expected disabled actually disabled, or still enabled with a message?

How: just call `inspect_screen("…")` with a narrow question — e.g. "Is the user menu dropdown open?",
"Did the dialog close?", "Did the form submit, or is an error shown?". You do NOT need to take a
screenshot first: `inspect_screen` captures the CURRENT page itself each time it runs, so its answer
always reflects the page as it is right now.

Make a vision check its OWN step: call `inspect_screen`, READ the answer, and only then decide your
next move. Never combine `inspect_screen` with a click, a navigation, or any other action in the same
turn — if you act while you ask, the screenshot it takes can be of the page you are moving TO, not the
one you meant to ask about, and the answer is then useless.

Every answer comes in two labeled parts: `Answer:` (your question — it will explicitly flag when the
question's premise doesn't match the page) and `On screen:` (what is actually rendered: the visible
heading/title, the main content, any dialog/overlay/banner/toast/error). ALWAYS read the `On screen:`
part: if it contradicts where you think you are or what you think is open, your mental model is wrong
— RE-ORIENT first (close the overlay, navigate back, log in again) before acting on anything else.
You never need to spend a separate call asking "what page am I on?" — every answer already tells you.

`inspect_screen` is for UNDERSTANDING the page only — is it open/closed, did it submit, is something
covering it, are you stuck, did the validation/disabled state actually appear. It NEVER returns a
selector, and you must NEVER ask it for one: do not ask for an `id`, a `data-testid`, a CSS/HTML
selector, or a locator. You ALWAYS have `browser_generate_locator` — capture EVERY selector with it
(Playwright reads the DOM; vision only reads pixels, and pixels carry no ids). Calls count against a
per-run budget, so spend them on these checkpoints (opening/closing menus & dialogs, submitting,
confirming a validation/disabled state) rather than idle looks or selector hunts.
