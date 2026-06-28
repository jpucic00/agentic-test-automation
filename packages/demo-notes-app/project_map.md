# Application Map — Demo Notes app

<!-- No selectors here on purpose: the Planner/Healer capture every locator LIVE from the
running app and pick the most robust kind the element supports (resilience ladder:
id > accessible > CSS > XPath). This map describes routes, flows, and quirks only. -->

## Base
- Base URL: http://localhost:3000 (local demo, non-production).
- Opening the base URL redirects to `/login`.
- Landing after login: `/notes`.
- Language: English only.

## Navigation (top navbar, present on every page)
| Element (by label/purpose) | Goes to / does                          | Visible to |
| -------------------------- | --------------------------------------- | ---------- |
| Brand "Demo Notes"         | `/` (which redirects to `/login`)       | everyone   |
| Login                      | `/login`                                | logged-out |
| Register                   | `/register`                             | logged-out |
| Logged-in email            | shows the current user's email          | logged-in  |
| Log out                    | clears the session, returns to `/login` | logged-in  |

## Auth flow (login) — step by step
1. Open the base URL → it redirects to `/login`.
2. Enter the email and password, then submit the login form.
3. On success the app navigates to `/notes`; the navbar shows the user's email and a Log out button.
4. On failure an error message appears and the page stays on `/login`.
5. Log out via the Log out control in the navbar.

## Registration flow — step by step
- Entry: `/register` (or click Register in the navbar).
- Enter email, password, and confirm-password; submit the form.
- Success: the account is created, the user is logged in automatically, and the app navigates to
  `/notes`.
- Failure: an error appears (passwords do not match, or the email already exists). Use a unique
  email per run.

## Routes & access by role
| Route       | Purpose                       | Auth                                     |
| ----------- | ----------------------------- | ---------------------------------------- |
| `/login`    | sign in                       | public                                   |
| `/register` | create an account             | public                                   |
| `/notes`    | list / create / edit / delete | logged-in (redirects to `/login` if not) |

## Key features

### Feature: Notes list
- Route: `/notes` (requires login).
- An empty state ("No notes yet…") shows when the user has no notes.
- When notes exist they render as one row per note, each row carrying the note's title and an
  edit and a delete control.
- To act on a specific note, find it by its visible title and use the control in that same row.

### Feature: Create / edit a note
- A "new note" control opens an editor with a title field, a body field, and save / cancel
  controls.
- Saving adds the new note to the top of the list.
- Editing opens the same editor (via the row's edit control), pre-filled with the note's values.

### Feature: Delete a note (confirmation dialog)
- The row's delete control opens a confirmation dialog (`role="dialog"`).
- The dialog has a confirm button (deletes) and a cancel button (closes without deleting); scope
  those locators to the dialog.
- After confirming, the note is removed; if it was the last one, the empty state returns.

## Known quirks
- Mixed accessibility ON PURPOSE (resilience-ladder fixture). Only the **login** page is fully
  id'd/semantic. Elsewhere: register/notes-editor inputs are label-only (no id → `getByLabel`);
  the New-note / Save / Cancel / per-row Edit / Delete / Log-out controls and the delete dialog's
  confirm/cancel are non-semantic `<div>`s with no role/id/aria (capture a verified CSS/XPath/text
  locator); note rows carry no per-row id. Climb the ladder per element — don't assume an id or a
  button role exists.
- Each run starts with an EMPTY `localStorage` (fresh browser). The seeded demo user
  (`demo@demo.test` / `Passw0rd!`) is re-created on every page load, so logging in as it always
  works; anything else (notes, extra accounts) must be created within the scenario.
- Per-note controls are generated per row at run time — locate a note by its visible title first,
  then use the edit/delete control in that row; don't rely on a fixed per-note identifier.
- Delete is guarded by a confirmation dialog; its confirm/cancel buttons exist only while the
  dialog is open.
- Login/registration error messages exist only after a failed submit.
