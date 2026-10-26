# Email in the hub: design (2026-10-25)

Kellen asked for an Outlook-style email client inside the hub: read, search, reply, compose and file mail without
leaving it, next to the CRM records the mail is about.

**Decided, 2026-10-25:** Kellen took the recommended answer to all nine questions in section 9 ("Go with the
recommended answers… and start step 1 (mail-connect) once PR #2 is merged"). Step 1 starts when PR #2 (`data-room`,
stacked on #1 `boards-bound`) is merged.

**Kellen, 2026-10-25:** "it shouldn't be required. Someone can turn it on and off." So Mail in the hub is **optional
for each person**. It is off until you switch it on in your own settings, and you can switch it off again at any time.
Nobody else's choice affects yours. Gmail itself stays exactly as it is: the hub is a second window onto the same
mailbox, and everything you do in one shows up in the other.

Browser notifications for new mail are being built in a separate session. This document only marks where new mail
would raise one ("Where notifications plug in").

---

## 1. What "like Outlook" means here

| Outlook has | The hub's version |
|---|---|
| Folder pane | Gmail's labels: Inbox, Starred, Sent, Drafts, Spam, Trash, plus your own labels. Unread counts on each. |
| Message list | One row per **thread** (conversation): sender(s), subject, first line, date, paperclip, unread dot, and chips for the CRM records it belongs to ("Firehawk · MEIH"). |
| Reading pane | The whole thread on the right, newest message open and older ones collapsed. Attachments listed under each message, opened or downloaded from Gmail. |
| Reply / Reply all / Forward | Yes, with attachments: forwarding keeps the originals and you can add files. Replies stay in the same Gmail thread. |
| New message | Compose window with To / Cc / Bcc, attachments, your signature, and **Insert template** (the outreach templates). Drafts save to Gmail as you type, so a draft started on the laptop is in Gmail on the phone. |
| Search | One box. It uses Gmail's own search (`from:`, `has:attachment`, `before:` and plain words all work), so it covers your whole mailbox, not only what the hub has seen. |
| Move / categories | Archive, Move to label, Add label, Mark read or unread, Star, Delete (to Trash). |
| Contact card | Click a sender to see their Person / Company card (stage, owner, last touch, open deals) in the reading pane's side strip. |
| "File to…" | **The CRM part.** One click files a thread to a company, deal, project, directory firm or lender, and it appears on that record's timeline. Each record's email entries get **Open in Mail** to jump back into the thread. |

Left out on purpose (Gmail on the web or phone still does these): rules and filters, vacation replies, Gmail settings,
calendar invites (accept or decline in Google Calendar; the Meetings page already reads it), and **permanent delete**
(the hub can only move mail to Trash).

### Layout

Desktop: three columns, like Outlook. Folders (narrow, can collapse), the thread list, the reading pane. Keyboard:
`j` / `k` next and previous, `r` reply, `a` reply all, `f` forward, `e` archive, `/` search, `c` compose. Everything
also works from buttons, so no shortcut is required.

Phone (narrow screens): one column at a time.
1. The list fills the screen, with the search box at the top and a label picker in place of the folder pane.
2. Tap a thread and it opens full-screen with a Back arrow. A bar fixed at the bottom has Reply, Reply all, Forward,
   File and Archive.
3. Compose opens as a full-screen sheet. Attach uses the phone's file picker, so photos and Files both work.
4. Record chips wrap onto a second line rather than widening the page. The UX second pass found 11 pages wider than a
   phone screen, so this gets a test that the page stays at phone width.

**On the phone it only works once the hub is hosted** (hub.acmigroup.com, waiting on billing). Today the hub runs on
the laptop, and Google only sends a sign-in back to `localhost` or a real https address, so a phone can't connect
Gmail to the laptop's hub. The phone layout is built and tested now so it's ready on the day hosting goes live.

### Long threads (Kellen: "threads that have over 80 messages… really hard to find attachments or the most recent message")

Gmail opens a long thread at the top, hides the middle behind a "62 older messages" bar, and leaves attachments
scattered through it. A deal thread with a lawyer, a broker and three redline rounds is exactly that. The hub's
reading pane works differently:

- **It opens on the newest message.** The latest message is shown first, open and in full. Older messages are folded
  underneath, newest to oldest. A **Newest first / Oldest first** toggle is remembered per person. Opening a thread
  with unread mail jumps to the first unread message, with a "New since you last read" line above it.
- **Attachments panel at the top of every thread.** One list of every file in the thread: name, type icon, size, who
  sent it, the date, and **Go to message**. It's newest first and searchable, with filters for PDF, Word, Excel and
  images.
  - **Versions grouped.** Files whose names differ only by a date, "v2", "redline", "clean" or "(1)" are grouped into
    one row, with the newest on top and "4 versions" to expand. "PSA - ACMI redline 10.21.docx" and "PSA - ACMI
    redline 10.24.docx" are one row, not two lost in 80 messages.
  - **Noise hidden.** Logos and signature images (small inline images, `image001.png`), calendar `.ics` files and the
    same file re-attached unchanged on a reply (same name and size) are hidden behind "Show all".
  - **Open or download** straight from the list; nothing is stored in the hub. "Download all" gives a zip of the
    newest version of each file.
- **Message outline.** A thin strip beside the thread (a tab on the phone) lists every message in one line each:
  sender, date, first words, and a paperclip if it has files. Click to jump. Filter it by person ("only Cozen's
  messages") or to "only messages with attachments".
- **Only the new words.** Each message shows just what that person wrote. The quoted history and the signature are
  folded away (the same `new_text` the timelines already use), which is most of what makes an 80-message thread
  long. **Show quoted text** brings them back.
- **Find in this thread.** A search box scoped to the thread, with matches highlighted and "3 of 12" arrows.
- **Jump to latest** is always on screen while you scroll an older part of the thread. On the phone it's a button
  above the bottom bar.
- **Speed.** The thread first loads every message's headers and attachment list only, enough for the outline and
  the attachments panel, so an 80-message thread opens in about a second. Bodies load as you open each message. The
  attachment list is kept in the private index, so the panel shows instantly next time.
- Gmail itself starts a new thread after 100 messages, often with the same subject. The hub notices a thread with
  the same subject and the same people and links it with "Earlier part of this conversation".

### Your Gmail labels and filters (Kellen: "if I have tags and filters set up on my gmail will that carry over?")

Yes. The hub isn't a copy of your mailbox: it's another window onto it.
- **Labels carry over, both ways.** Every label you have in Gmail shows in the hub's folder pane, nested the same
  way and with the same colours. A label you add or remove in the hub shows in Gmail on your phone a moment later,
  and the other way round. The same goes for Gmail's own tabs (Primary, Promotions, Updates), stars and Important.
- **Filters keep working, because Gmail runs them.** A filter acts the moment mail arrives, before the hub sees it.
  Mail your filters label, archive or star turns up in the hub already labelled, archived or starred.
- **What the hub doesn't do is edit filters.** Creating or changing a filter needs a separate Gmail settings
  permission, which isn't worth asking for. Filters stay in Gmail's settings, where they are today. An **Open in
  Gmail** link takes you to the thread in Gmail, where More › Filter messages like these sets one up.

### CRM labels on your mail (Kellen: "the emails should be tagged with company or project labels as it makes sense")

Every thread gets **record chips** in the hub: the company, deal, project or campus, lender or directory firm it's
about. They come from the same matching the timeline sync already does (sender's domain, the people on the thread,
project keywords like "MBI B101", and anything you filed by hand).

The same tags also become **real Gmail labels**, so they show in Gmail on your phone and work in Gmail search
(`label:acmi-meih`). They're nested under one parent so they don't mix with your own labels:

```
ACMI/
  Projects/MEIH            ACMI/Projects/The Forge          (every campus and project)
  Tenants/Firehawk Aerospace                                (companies with an open deal, or a thread you filed)
  Capital/Shore United Bank                                 (lenders and investors)
  Directory/Cozen O'Connor                                  (consultants, lawyers, vendors you filed)
```

- **Only where it makes sense.** Campuses and projects always get a label (a few dozen). Companies get one only when
  they have an open tenant deal, or once you file a thread to them. Labelling all 1,769 companies would bury your
  own labels.
- **Hands-off for Gmail's own rules.** The hub adds these labels to threads as the sync matches them, for the last
  90 days and going forward. If you take a CRM label off a thread in Gmail, the hub reads that as "not about this"
  and doesn't put it back.
- **Renames follow.** Renaming a company or project in the hub renames its label in Gmail.
- **Your switch.** In Mail in the hub, **Add CRM labels in Gmail** is on by default and can be switched off on its
  own. The chips in the hub stay either way.
- The same labels, read the other way: putting a thread under `ACMI/Projects/MEIH` in Gmail on your phone files it
  to MEIH in the hub, just like **File to…**.

### Ideas taken from other email clients (Kellen: "anything from other top email clients that should inspire us?")

| Idea | From | What it is in the hub | Step |
|---|---|---|---|
| **Split inbox** | Superhuman | Tabs above the list: **Tenants**, **Projects**, **Capital & directory**, **Everything else**, built from the CRM chips. The deal mail no longer sits between newsletters. | 3 |
| **Thread summary** | Shortwave, Gemini in Gmail | A **Summarize** button at the top of a long thread: where it stands, open asks and who owes what, and the latest version of each document, with Claude citing the messages. Only when you click (it sends that thread's text to Anthropic, like the hub's other Claude features). | 3b |
| **Files view** | Outlook | Every attachment in your mailbox in one searchable list, filterable by company or project ("every PSA redline for The Forge"), using the same version grouping as a thread. | 3b |
| **Snooze** | Gmail, Superhuman | "Back at 8am Monday": the thread leaves the inbox and comes back on time. It uses a Gmail label, so it's snoozed on the phone too. | 4 |
| **Remind me if no reply** | Superhuman, Gmail nudges | When sending: "remind me in 3 days if they don't answer". If nobody replies, it shows in My day and sets the deal's ball in their court. | 5 |
| **Comments and assign** | Front, Missive | A private team note on a thread ("Simon, can you take the survey questions?") with @mentions, using the hub's existing comments and bell. Only teammates you mention, or who can already see the thread, see it. | 6 |
| **Screener** | HEY | Already there: Mail review is a screener for unknown senders. The inbox shows a "New sender" tag that links to it. | 3 |
| **Send later** | Spark, Gmail | Wait for hosting. Gmail's API has no scheduled send, so the hub would have to stay awake to send it, and a sleeping laptop would miss it. | 9 |

Not taken: read receipts and open tracking (the hub blocks other people's tracking pixels, so it won't add its own),
an AI that writes or sends replies on its own, and Outlook's "Focused" sorting (Gmail's tabs and the split inbox
already do that job).

---

## 2. How it connects to Gmail

Two ways are possible. **Recommended: the Gmail API with each person's own Google sign-in.** The decision table at
the top of `HANDOFF.md` already says this ("Per-user Gmail API via Google sign-in, replacing the shared app
password").

| | Gmail API + Google sign-in (recommended) | IMAP / SMTP + app passwords (what the timeline sync uses today) |
|---|---|---|
| Turning it on | Click **Turn on Mail**, approve Google's screen once. Works for anyone, from any device once hosted. | Each person makes a 16-letter app password and someone pastes it into `.env` on the laptop. |
| What the hub can do | Only what was approved (read, label, draft, send). No permanent delete. Revoke it any time from Google Account › Security, or by switching Mail off in the hub. | An app password is the whole account: full mailbox access, and it can't be narrowed. |
| Labels, threads, search | Native: thread ids, labels, Gmail search syntax, drafts. | Possible through Gmail's IMAP extensions, but every action is a round trip on a long-lived connection, capped at 15 connections per account (shared with the phone). |
| New mail | Incremental "what changed since…" list (cheap, every minute). Instant push once hosted. | Poll by IMAP id; the current sync already needs 8 connections and retries. |
| Sending | Same API, and the email lands in Sent and the thread automatically. | SMTP; works, but it's a second password-based connection. |
| Risk | Tokens are as sensitive as passwords, so they're encrypted (section 5). | App passwords sit in `.env` in plain text, and Google keeps tightening them. Rotating the current one is already on Kellen's list. |

### Scopes

One extra Google permission, asked **only when someone turns Mail on**: `https://www.googleapis.com/auth/gmail.modify`.
It covers reading, labels, archive, Trash, drafts and sending, and it **excludes permanent deletion**. Everyone else
keeps today's sign-in (profile, email, `calendar.readonly`). This uses Google's incremental consent
(`include_granted_scopes=true`): the Gmail permission is added to the person's existing grant, and sign-in for
everyone else stays as it is. The broader `https://mail.google.com/` scope (full IMAP-level access, including
permanent delete) is not needed and won't be asked for.

### Google's verification

Gmail's read and modify scopes are "restricted". For an app open to the public, that means Google's verification plus
a yearly independent security assessment (CASA), which takes weeks and costs money. **An *Internal* app is exempt.**
The hub's OAuth client lives in Google Cloud project `sincere-passage-511621-j3`, under the acmigroup.com organization,
and sign-in already only accepts acmigroup.com accounts. If its consent screen is set to **Internal**, no verification
or assessment is needed. Three things to check before building (section 9, "Access"):
- The consent screen's **User type is Internal**. If it was set up as External, switch it; an External app in
  "Testing" mode also drops refresh tokens after 7 days, which would break the background sync.
- **acmiproperties.com.** The hub treats it as internal mail, but an Internal app only serves accounts in the same
  Google Workspace. If acmiproperties.com is a secondary domain of the acmigroup.com Workspace, it works. If it's a
  separate Workspace, those accounts can't connect.
- **Workspace Admin › Security › API controls.** If the admin has restricted Gmail access for third-party apps, the
  hub's client has to be marked Trusted.

The Gmail API has to be switched on in that Cloud project (APIs & Services › Library › Gmail API). It's free and
doesn't need billing. Quota is 250 units per person per second, far above what one person reading mail uses.

---

## 3. What the hub stores and what it fetches live

Gmail stays the record. The hub keeps only enough to draw the list quickly and to tie mail to CRM records.

| Kept in the hub | Fetched from Gmail when needed |
|---|---|
| **A private index of your threads**: subject, participants, first line (Gmail's snippet), date, labels, unread / starred, has-attachments, Gmail's change marker. The last 90 days, plus any older thread you open or search for. | **Message bodies**: fetched when you open a thread, then held in memory for a few minutes only. |
| Your label list and unread counts. | **Attachments**: streamed through the hub when you click, never saved to disk. |
| **Filing links**: which thread is filed to which record, who filed it, when. | **Search**: run by Gmail across your whole mailbox. Results join the index. |
| The **timeline entries** the sync already writes (unchanged, see section 4). | **Drafts**: saved in Gmail, not in the hub. |
| Your encrypted Google token. | |

Size: Kellen's mailbox has about 55,000 messages a year. At roughly 1 KB of index per thread, 90 days is a few MB per
person.

Switching Mail off deletes your index and your token. Filing links and timeline entries stay, because those belong
to the CRM records (the same as today's sync).

---

## 4. How it fits the existing sync (nothing logged twice)

Today the **timeline sync** (`crm/mail/sync.py`, `sync_mail`, every 15 minutes) reads each `Mailbox` over IMAP and
writes one `Activity` per email per company, keyed `gmail:<Message-ID>|c<company>`. Unknown domains go to Mail review,
directory firms' mail to `FirmEmail`. Outreach (`crm/outreach/sender.py`) logs each send at once with the same key, so
the sync later sees it as "already logged".

The client is built so that rule keeps holding.

1. **One reader per mailbox.** Once a person turns Mail on, their mailbox switches from IMAP to the API reader
   (`Mailbox.backend`: `imap` or `gmail_api`). HANDOFF anticipated this: "switching to the Gmail API … only replaces
   `crm/mail/imap.py`". The new `crm/mail/gmail_api.py` produces the same `ParsedEmail` objects and hands them to the
   same `process_messages`. Classification, matching, Mail review, the directory, stages and project tagging don't
   change.
2. **One pass feeds both.** Each refresh asks Gmail what changed since the stored marker (`history.list` from
   `Mailbox.history_id`). The same pass updates the private index *and* sends new messages through the timeline
   recorder, under the mailbox's existing sync lock. Opening the Mail page triggers a refresh; the 15-minute task
   keeps going for everyone else. If a refresh is already running, the page shows the index as it is.
3. **The same keys.** The API gives the RFC `Message-ID` header, so timeline keys are identical whichever reader saw
   the email. Gmail's API gives thread ids in hex, while IMAP's `X-GM-THRID` is the same number in decimal. The reader
   converts to decimal, because reply detection, project tagging and the weekly report all match on the stored
   `thread_id`.
4. **The switch-over.** When a mailbox moves from IMAP to the API, the reader starts at the moment of the switch and
   reads back to the last IMAP sync time. The overlap is harmless: rows already there are counted as "already
   logged". This gets a test on a scratch copy: activity counts before and after a switch must match.
5. **Mail sent from the hub** is logged the moment it's sent, keyed exactly like the sync (as outreach does), with the
   right person, company and thread. When the sync later reads it in Sent, it's "already logged".
6. **Filing** is the one new way mail reaches a record. When the sync already logged a thread, filing adds the record
   link (and the deal or project, below). When the sync skipped it (a gmail.com sender, an unknown domain still in
   Mail review, an internal thread), filing writes the timeline entries for that thread's messages with the same keys.
   For a domain in Mail review it offers the existing **Add as company** / **attach** step (`approve_domain`), so the
   rest of that domain's waiting mail follows. Filing a lender's or consultant's thread files it on the directory firm
   (`FirmEmail`), never as a Company. That keeps lenders out of outreach, as Kellen asked on 2026-10-21.
7. **Projects.** Today `crm/projects.py` re-derives a thread's project tags from keywords each time it runs. A thread
   filed to a project by hand gets a manual link that the tagger keeps, so a later run can't take it off.
8. **Attachments on timelines** stay name-and-size only (`Attachment`), now with Gmail's attachment id. **Open** on a
   timeline attachment then works for people who have that mail in their own mailbox, or a grant to a shared one.

---

## 5. Security and privacy

**Each person sees only their own mailbox**, plus shared mailboxes they've been given (section 6). That includes
Managers and admins: being a Manager gives no access to anyone else's mail. Concretely:
- Every Mail page and API call finds mail through *the signed-in person's* mailbox or grants, never through an id in
  the address. A thread id that isn't yours returns "not found", the same as one that doesn't exist.
- The private index is not in Django admin, not in hub search (Ctrl K), and not in exports.
- What the team sees doesn't change: the timeline entries the sync already writes for external CRM mail, and threads
  someone chose to file. Filing is the deliberate step that shares a thread with the team.

**Capital partners can never reach it.** People in the **Partners** group (the data room's accounts) are kept out at
each layer:
1. `crm.middleware.PartnerOnlyMiddleware` already sends a partner's account to `/room/` from every other page,
   `/mail/` included.
2. The Mail views also check `is_partner()` themselves and return "not found", so a future change to the middleware
   can't open them by accident.
3. **Turn on Mail** and the Gmail connect step refuse partner accounts, so no partner can ever hold a Gmail token in
   the hub.
4. The pickers for sharing a mailbox list only `permissions.team_users()`, so a partner can't be granted one.
5. The 15-minute sync only reads mailboxes whose owner is in `team_users()`.
Each layer gets its own test.

**Tokens encrypted at rest.** Today allauth stores the Google tokens (`SocialToken`, used by the calendar sync) as
plain text in `db.sqlite3`. "Encrypt the Google tokens at rest" is already on the Waiting list. With Gmail access in
the token, it moves to step 1 of the build:
- Tokens are encrypted with a key from `.env` (`ACMI_TOKEN_KEY`; on Cloud Run, Secret Manager). Existing calendar
  tokens are encrypted in the same migration. A copy of the database (a backup, say) is useless without the key.
- The key never goes in the database, the repo, a backup or a log. Without it, people simply reconnect.
- Turning Mail off, or an admin deactivating an account, revokes the token at Google and deletes it.
- Still on Kellen's list, and more important with mail on the laptop: turn on **device encryption** on the laptop,
  and rotate the old app password once outreach moves to the API (step 8).

**Showing email safely.**
- Message HTML is cleaned (the allow-list approach of `crm/news/drafts.py sanitize_html`) and shown in a sandboxed
  frame that can't run scripts or reach the hub's pages.
- **Remote images are blocked** until you click Show images, so senders' tracking pixels don't report that you opened
  their mail. You can trust a sender to always show theirs.
- Links open in a new tab, without sending the hub's address along.
- Attachments always download as files; an HTML or SVG attachment is never shown inline.

**Sending safely.**
- Nothing is ever sent by the hub on its own. Every email is one person's click.
- **Undo send**: Send starts a 10-second countdown in the page with an Undo button. Closing the page during the
  countdown cancels it.
- Sends are logged in the hub (who, when, to whom) next to the timeline entry.
- A site-wide switch, `ACMI_MAIL_SEND=false`, blocks sending entirely. It stays off on test and scratch databases, and
  the tests mock Gmail, so no test can send real mail.

**Site-wide on/off.** `ACMI_MAIL_CLIENT=false` hides Mail for everyone and stops asking for the Gmail permission. Each
person's own switch sits under it.

---

## 6. Shared mailboxes

Google Workspace has no Outlook-style shared mailbox. Recommended approach: a shared address (an account like
`deals@`) is connected **once**, by someone who can sign in as it, using the same Turn on Mail step. A Manager then
gives named teammates access (**read**, or **read and send as**) from a list built from `team_users()`. In the hub it
appears as a second mailbox under your own. Every open and send in a shared mailbox is logged with who did it.

Not recommended: Google's domain-wide delegation, which would let the hub read *any* mailbox in the company without
asking. That's too much power for a laptop-hosted app.

Google Groups "collaborative inboxes" aren't real mailboxes and the Gmail API can't read them. A group whose mail
should be shared has to deliver to a real account first.

---

## 7. Where notifications plug in

New mail arrives through one place: the refresh pass in step 2 of section 4. When it sees new messages with the
INBOX label, it calls `crm.mail.client.on_new_mail(mailbox, threads)`. That's the hook for the notifications session:
a browser notification for each person with access to that mailbox, respecting their own on/off choice. The existing
in-app bell rule ("a company you own replied") keeps working from the timeline as it does today.

---

## 8. Build order (one branch each, tests and CI, per "How to work in this repo")

Migrations take the next free number when each step is built (browser notifications took 0060; step 1 is **0061**). The client
needs `team_users()` and the Partners group from #2, so step 1 waits for #2 to merge (or branches from it).

1. **`mail-connect`** (built 2026-10-25 on branch `mail-connect`; details in HANDOFF): the on/off switch per person (in your settings; Mail hidden when off) and the site-wide one;
   encrypted tokens (including today's calendar tokens), revoke on switch-off; the Turn on Mail step with incremental
   `gmail.modify` consent; partner refusal at every layer; `Mailbox.backend` and `history_id`. No mail on screen yet.
   Migration 0061.
2. **`mail-api-reader`** (built 2026-10-25 on branch `mail-api-reader`; details in HANDOFF): `crm/mail/gmail_api.py` reading changes since the marker into the existing
   `process_messages`, hex-to-decimal thread ids, the switch-over back-read, IMAP kept for anyone not switched. Proof
   on a scratch copy that switching logs nothing twice.
3. **`mail-inbox`** (built 2026-10-26; details in HANDOFF) (read only): labels, the thread list from the private index, the reading pane (cleaned, sandboxed,
   images blocked), Gmail search, attachment download, record chips, the split inbox (Tenants / Projects / Capital &
   directory / Everything else), the "New sender" tag, the phone layout and its width test. A migration (the
   index).
   **3b. `mail-long-threads`** (built 2026-10-26; details in HANDOFF): the long-thread reading pane from section 1: newest message first, the attachments
   panel with versions grouped and noise hidden, the outline with person and attachment filters, find in thread, jump
   to latest, and "Earlier part of this conversation". Tested on an 80-plus-message thread of made-up mail. Also
   **Summarize** (Claude, on click) and the **Files** view across the whole mailbox.
4. **`mail-actions`** (built 2026-10-26; details in HANDOFF): archive, read / unread, star, labels and move, Trash, snooze.
5. **`mail-compose`** (built 2026-10-26; details in HANDOFF): new message, reply, reply all, forward with attachments, Gmail drafts, templates, signature,
   undo send, remind me if no reply, immediate timeline logging. **Email** buttons on person and company pages open compose with the address
   filled in.
6. **`mail-filing`** (built 2026-10-26; details in HANDOFF): File to company / deal / project / directory firm / lender, the manual project link, Mail review
   hand-off for unknown domains, **Open in Mail** from timelines, **CRM labels in Gmail** (`ACMI/…`, both ways, with
   its own switch), and team comments on a thread. A migration.
7. **`mail-shared`** (built 2026-10-26; details in HANDOFF): shared mailboxes, grants, access log. A migration.
8. **`mail-outreach-api`**: outreach batches, test sends, the weekly report, the campus email and the news-review
   drafts move to the Gmail API. Then the app password can be revoked.
9. **When hosted** (billing on, hub.acmigroup.com): instant new mail through Gmail push (Cloud Pub/Sub), the Cloud Run
   address added to the OAuth client, and Mail on the phone.

Steps 1 to 3 give a working read-only inbox. Each later step stands on its own.

---

## 9. Kellen: decisions and access

### Decisions (recommended answer first)

1. **Connect through Google sign-in (Gmail API), not app passwords?** Recommended: yes, Google sign-in.
2. **Off until each person turns it on?** Recommended: yes. That's what you asked for. It's off for everyone at
   first, including you.
3. **Keep only a 90-day index in the hub, and read full mail from Gmail when opened?** Recommended: yes. Older mail
   is still reachable through search.
4. **Should filing a thread put it on the record's timeline, where the whole team sees it?** Recommended: yes. Mail
   you don't file stays private, apart from what the timeline sync already logs today.
5. **Shared mailboxes: which addresses, if any?** Recommended: none for now. Name any (e.g. `deals@`, `info@`) before
   step 7.
6. **Undo send with a 10-second countdown?** Recommended: yes.
7. **Block images in email until you click Show images?** Recommended: yes.
8. **No permanent delete from the hub, Trash only?** Recommended: yes.
9. **Once you're on the API, move outreach sending to it too and retire the app password (step 8)?** Recommended: yes.

### Added 2026-10-25, after the first answers (recommended answer first)

10. **Put the CRM tags on your mail as real Gmail labels under `ACMI/`, on by default with its own off switch?**
    Recommended: yes. Campuses and projects always; companies only with an open deal or once filed.
11. **Build the ideas in the table in section 1 (split inbox, Summarize, Files view, snooze, remind if no reply,
    team comments), each in the step shown?** Recommended: yes. Send later waits for hosting.

### Access needed

- **Google Cloud console**, project `sincere-passage-511621-j3` (you or whoever owns it): turn on the Gmail API;
  confirm the OAuth consent screen is **Internal**; add the `gmail.modify` scope to the consent screen. Free, no
  billing needed. Claude can do this with the signed-in `gcloud` if you say so.
- **Google Workspace admin** (a super admin): confirm acmiproperties.com is in the same Workspace as acmigroup.com,
  and that the hub's OAuth client is allowed under Security › API controls.
- **An encryption key in `.env`** (`ACMI_TOKEN_KEY`): Claude generates it and writes it without printing it. Keep a
  copy in the password manager; losing it only means everyone reconnects.
- **Each person who wants Mail**: click Turn on Mail and approve Google's screen once.
- **For shared mailboxes (later)**: someone who can sign in to each shared account, once.
- **Still open from before**: turn on device encryption on the laptop; turn on billing for hosting (the phone and
  instant push wait on it).
