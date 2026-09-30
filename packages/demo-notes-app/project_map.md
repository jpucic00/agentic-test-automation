# Application Map — Demo Notes app

## Base
- Base URL: http://localhost:3000 (local demo).
- Opening the base URL takes you to the "Log in" page.
- After logging in you land on the "Your notes" page.
- Language: English only.

## Navigation (top bar, on every page)
| Item                | What it does                                        | Visible to  |
| ------------------- | --------------------------------------------------- | ----------- |
| "Demo Notes" (logo) | Goes to the start page (the "Log in" page)          | everyone    |
| Login               | Opens the "Log in" page                             | logged out  |
| Register            | Opens the "Create an account" page                  | logged out  |
| About note-taking   | Opens the Wikipedia article "Note-taking" (leaves the app, same tab) | everyone |
| Your email address  | Shows who is logged in (not clickable)              | logged in   |
| Log out             | Logs you out and returns to the "Log in" page       | logged in   |

## Auth flow (login) — step by step
1. Open the base URL → the "Log in" page appears.
2. Fill in Email and Password, then click "Log in".
3. Success: the "Your notes" page opens and the top bar shows your email and "Log out".
4. Wrong email or password: the message "Invalid email or password." appears and you stay on
   the "Log in" page.
5. To log out, click "Log out" in the top bar.

## Registration flow — step by step
- Open it via "Register" in the top bar (or the "Register here" link on the "Log in" page).
- Fill in Email, Password, and Confirm password, then click "Register".
- Success: you are logged in straight away and land on "Your notes" (empty for a new account).
- Failure: an error message appears and you stay on the page —
  "Passwords do not match." or "An account with that email already exists."

## Pages you can open directly
| Address     | Page                | Who can open it                                   |
| ----------- | ------------------- | ------------------------------------------------- |
| `/login`    | Log in              | everyone                                          |
| `/register` | Create an account   | everyone                                          |
| `/notes`    | Your notes          | logged-in users (otherwise you're sent to Log in) |

## Key features

### Your notes (list)
- With no notes, the page shows a "No notes yet" message.
- Each note is shown with its title, its body text, and its own "Edit" and "Delete" buttons.
- To work on a specific note, find it by its title and use the "Edit" / "Delete" next to it.

### Create / edit a note
- Click "New note": a form opens with Title and Body fields and "Save note" / "Cancel".
- "Save note" adds the note to the list; "Cancel" closes the form without saving.
- "Edit" on a note opens the same form already filled in with that note; "Save note" updates it.

### Delete a note
- "Delete" on a note opens a "Delete note" confirmation box asking you to confirm
  ("… This cannot be undone.").
- In that box, "Delete" removes the note; "Cancel" closes the box and keeps the note.
- While the box is open, the word "Delete" shows up four times on the page: the note's own
  "Delete" button, the heading "Delete note", the question, and the confirm button. The confirm
  button is the one right next to "Cancel" inside the box — work inside the box, not the whole page.
- After deleting the last note, the "No notes yet" message comes back.

### About note-taking (external link)
- Takes you to the Wikipedia "Note-taking" article in the same tab. Wikipedia is not part of the
  app — only check that the article opened; don't test the site itself.
