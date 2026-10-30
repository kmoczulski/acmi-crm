# Mail dock: email boxes along the bottom of the page (built 2026-10-29, branch `mail-dock`, not merged)

Kellen: "if I click an email notification or start a new email it should open up like it does in email from the bottom of
the screen in its own little box." Before this, a new message, a Forward and the ✉ Email buttons on people and companies
opened the full compose page, and clicking a new-email alert (toast, bell list or browser notification) navigated to the
inbox, so you lost the page you were on.

## What it does

- **New message / Forward / ✉ Email** (any link to `/inbox/compose/`) opens a **box** at the bottom right of whatever page
  you're on, like Gmail's compose window: To, Cc / Bcc, Subject, the formatting bar, 📅 Times, attachments, Send with the
  10-second Undo, Discard. The page behind it stays where it was.
- **A new-email alert opens the conversation in a box**: the toast, the bell's list and a browser notification. The box
  shows the messages (newest first, with attachments), **Reply / Reply all / Forward** (each opens a compose box beside it)
  and **Open in Mail**. Reading it marks it read, as in the inbox, and clears the alert.
- Each box has **– minimize** (click the title too), **⤢ bigger**, **✕ close**. Closing a compose box keeps what was typed
  as a draft in Gmail ("Draft saved."); **Discard** deletes the draft. Sending closes the box and says "Message sent." with a
  **View** link. Up to 4 boxes; when they won't fit the window the oldest fold to a title bar.
- **Boxes follow you from page to page** (kept for the browser tab, `sessionStorage`): the next page you open shows them
  again with what you'd typed, and autosave goes on updating the same Gmail draft. Attached files can't be kept; the box says
  "Attach again: …".
- 📅 Times opens its calendar panel on the page beside the box, and the boxes move left of it (and left of a split-view
  pane). The reading pane's own inline reply in the inbox is unchanged (its Reply buttons still open it in place). A phone
  gets one full-screen box and folded bars along the bottom.
- The split view's side frame hands its New message / alert links up to the page, so the box opens on the main side.
- A shared mailbox's compose page now says "Sending from the shared mailbox …" (the session's mailbox is sticky, so a
  box opened from an alert in a shared mailbox sends from it until you switch back).

## How it's built

- `static/crm/dock.js` (loaded by `base.html` for people with Mail on): the dock, the click routing, restore on the next
  page, `window.acmiDock` (`compose`, `thread`, `title`, `sent`, `close`). A box is a frame of a hub page drawn without
  the top bar (`window_base.html`): `/inbox/compose/?window=1&w=<id>` for writing (`compose.html` with `window` set) and
  `/inbox/window/?t=<thread>` for reading (`inbox_views.thread_window`, `mail_window.html`, `static/crm/mail_window.js`).
  Only those two paths are ever framed, whatever the address bar, storage or a notification hands the dock.
- `static/crm/compose.js` gained a third kind of form, `'window'`: JSON send and discard (`inline=1&dock=1`; the server
  answers `{ok, url}` and leaves no "Sent." flash for a later page), a draft id kept across pages (`hubDock:<id>`), the
  box's title following the subject, Ctrl+Enter to send, and `window.acmiWindow` (`sending()`, `flush()`) for the dock to ask
  before it closes. The Times panel is drawn on the hub page when the form is in a box.
- Alerts: `notify.window_url(note)` turns a new-email alert's inbox link into `/inbox/window/?[mb=…&]t=…`. The poll
  (`notification_poll`), the bell's list (`data-window`) and the browser-notification payload (`push.payload`) carry it.
  The service worker (`/sw.js`) tells an open hub page to open the box (`postMessage {type: 'hub-window'}`); with no hub
  page open it opens `/?dock=<box address>&read=<alert address>`, which the dock consumes and strips from the address.
- No migration, no new settings. 27 tests in `crm/tests/test_mail_dock.py`.

## Checked, and not checked

Checked in the in-app browser against a scratch database and the tests' fake Gmail (never the real database): opening a
box from a person page and from the inbox's New message, typing, the box and its text coming back on the next page, an
alert from the bell and from the live toast opening a conversation box, Reply from it, the 10-second send, the Times
panel and inserting a slot, close with a draft, dark mode, a 375 px phone, the split side frame, and the service-worker
message and `?dock=` routes (with a synthetic message; bad addresses ignored). Django tests: compose / inbox / push / alerts.

**Not checked:** a real browser notification click (the in-app browser blocks the permission), Safari, Firefox, a real
phone, and anything against real Gmail (the send, draft and Times calls are the existing ones, but not exercised here).
After deploying, an already-open hub tab keeps the old script until it's reloaded, so its first notification click still
navigates.

## Ideas this makes easy

Google Chat is built this way: two more kinds of box in `dock.js` (`chat`, and the pinned `chats` list), pages drawn with
`window_base.html` (`docs/CHAT.md`). A signature-status box could be another: add a `kind` in `dock.js` and a page.
