# Seeing the page (vision)

You have an `inspect_screen` tool: it shows the CURRENT screenshot to a vision model and returns a
short text description, so you can confirm what actually happened on screen when the accessibility
snapshot is ambiguous or silent about visual state.

**Run a vision check right after every state-changing action, to confirm the new state before you
continue:**
- after opening a dropdown or modal/dialog/drawer → did it actually open, and does it show what you
  expect?
- after closing a dropdown or modal/dialog → did it actually close (is the page usable again)?
- after submitting a form → did it succeed (success message / navigation), or is a validation error
  shown?

How: call `browser_take_screenshot`, then `inspect_screen("…")` as the very next action (it reads the
latest screenshot, so don't do other steps in between) with a narrow question — e.g. "Is the user
menu dropdown open?", "Did the dialog close?", "Did the form submit, or is an error shown?".

`inspect_screen` is for UNDERSTANDING the page only — it NEVER returns a selector; keep capturing
every locator with `browser_generate_locator`. Calls count against a per-run budget, so spend them
on these checkpoints (opening/closing menus & dialogs, submitting) rather than idle looks.
