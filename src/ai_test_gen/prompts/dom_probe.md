# Finding elements the snapshot can't name (DOM probe)

You have a `probe_dom(text, scope?)` tool: it searches the live DOM (read-only) for elements whose
visible text or attributes match `text`, and returns each match's real tag, id, classes,
attributes, and visibility — plus a CANDIDATE CSS and XPath selector with match counts. Use it when
the accessibility snapshot does NOT usefully show an element you can see or expect: non-semantic
div/span controls, an unnamed button in a modal, a sparse or empty snapshot. Pass `scope` (a CSS
selector, e.g. of the open dialog) to search only inside a container.

- The snapshot + `browser_generate_locator` remain your PRIMARY path — reach for `probe_dom` when
  that path fails you, not first.
- Candidates are RECONNAISSANCE, not locators of record. Before recording or using one, VERIFY it:
  pass the candidate as `browser_generate_locator`'s `target`, and/or confirm with
  `browser_verify_element_visible`. NEVER record an unverified candidate.
- A match with `inIframe` set lives inside an embedded frame — plain page locators cannot reach it;
  record that fact in `notes` instead of forcing a selector. `crossOriginIframes` lists frames the
  probe cannot see into at all.
- Calls are budgeted per run: spend them on elements the snapshot genuinely can't name, not on ones
  `browser_generate_locator` already handles.
