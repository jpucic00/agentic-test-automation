# Seeing the page (vision)

You have an `inspect_screen` tool: it captures the CURRENT page and shows that screenshot to a vision
model, returning a short text description — so you can confirm what actually happened on screen when
the accessibility snapshot is ambiguous or silent about visual state.

**Run a vision check right after every state-changing action, to confirm the new state before you
continue:**
- after opening a dropdown or modal/dialog/drawer → did it actually open, and does it show what you
  expect?
- after closing a dropdown or modal/dialog → did it actually close (is the page usable again)?
- after submitting a form → did it succeed (success message / navigation), or is a validation error
  shown?

How: just call `inspect_screen("…")` with a narrow question — e.g. "Is the user menu dropdown open?",
"Did the dialog close?", "Did the form submit, or is an error shown?". You do NOT need to take a
screenshot first: `inspect_screen` captures the CURRENT page itself each time it runs, so its answer
always reflects the page as it is right now.

`inspect_screen` is for UNDERSTANDING the page only — is it open/closed, did it submit, is something
covering it, are you stuck. It NEVER returns a selector, and you must NEVER ask it for one: do not ask
for an `id`, a `data-testid`, a CSS/HTML selector, or a locator. EVERY selector comes only from
`browser_generate_locator` (Playwright reads the DOM; vision only reads pixels, and pixels carry no
ids). Calls count against a per-run budget, so spend them on these checkpoints (opening/closing menus &
dialogs, submitting) rather than idle looks or selector hunts.
