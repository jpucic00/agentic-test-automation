# Declared follow-up (activation) flows

The Project Context or Application Map declares a mandatory follow-up for some records a test
creates — the canonical example: a newly registered account must be ACTIVATED via an
email-verification link on the mail-catcher UI the map lists before its first login works.

- The follow-up's steps are REAL PLAN STEPS: perform them live, in order, right after the creation
  step — go to the declared tool (its URL is directly addressable), find the newest message or
  item for the record you just created, complete the verification, and capture selectors there
  like on any page.
- NEVER log in with (or otherwise use) a created record before its declared activation flow
  completes.
- **A freshly-created account can't log in:** a failed first login on a fresh account usually
  means the activation was skipped, not that a selector is wrong — it can look exactly like a
  wrong password. If the test creates the account and never activates it, ADD the missing
  activation steps between creation and first login, reproduced live.
