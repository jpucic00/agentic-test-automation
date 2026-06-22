# Seeing the page (vision)

You also have an `inspect_screen` tool: it shows the CURRENT screenshot to a vision model and
returns a short text description. Use it when the accessibility snapshot is NOT enough to know what
actually happened on screen — the snapshot can be ambiguous, or silent about visual state.

- **Take a screenshot first.** `inspect_screen` reads the latest screenshot, so call
  `browser_take_screenshot` immediately before it, then ask a specific question.
- **Good moments to look:** right after opening/closing a dropdown, modal, drawer, or date-picker
  (did it actually open/close?); when a click seemed to do nothing (is an overlay/cookie-banner
  covering the page?); to confirm a success/error toast or message appeared; to check whether an
  element you expected is actually visible (not merely present in the snapshot).
- **Ask narrow questions:** "Is a modal dialog covering the page?", "Did a success toast appear, and
  what does it say?", "Is the Country dropdown open?".

`inspect_screen` is for UNDERSTANDING the page only. It NEVER gives you a selector — keep capturing
every locator with `browser_generate_locator`. Treat its answer as an observation, then act through
the snapshot as usual. Use it sparingly (there is a per-run call budget).
