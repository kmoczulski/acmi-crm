# Email in the hub: design (2026-10-25)

Kellen asked for an Outlook-style email client inside the hub: read, search, reply, compose and file mail without
leaving it, next to the CRM records the mail is about. This is the design only. No code is written yet. Kellen answers
the questions at the end first.

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

PRs #1 (`boards-bound`) and #2 (`data-room`) add migrations 0057 to 0059, so these number from **0060**. The client
needs `team_users()` and the Partners group from #2, so step 1 waits for #2 to merge (or branches from it).

1. **`mail-connect`**: the on/off switch per person (in your settings; Mail hidden when off) and the site-wide one;
   encrypted tokens (including today's calendar tokens), revoke on switch-off; the Turn on Mail step with incremental
   `gmail.modify` consent; partner refusal at every layer; `Mailbox.backend` and `history_id`. No mail on screen yet.
   Migration 0060.
2. **`mail-api-reader`**: `crm/mail/gmail_api.py` reading changes since the marker into the existing
   `process_messages`, hex-to-decimal thread ids, the switch-over back-read, IMAP kept for anyone not switched. Proof
   on a scratch copy that switching logs nothing twice.
3. **`mail-inbox`** (read only): labels, the thread list from the private index, the reading pane (cleaned, sandboxed,
   images blocked), Gmail search, attachment download, record chips, the phone layout and its width test. Migration
   0061 (the index).
4. **`mail-actions`**: archive, read / unread, star, labels and move, Trash.
5. **`mail-compose`**: new message, reply, reply all, forward with attachments, Gmail drafts, templates, signature,
   undo send, immediate timeline logging. **Email** buttons on person and company pages open compose with the address
   filled in.
6. **`mail-filing`**: File to company / deal / project / directory firm / lender, the manual project link, Mail review
   hand-off for unknown domains, and **Open in Mail** from timelines. Migration 0062.
7. **`mail-shared`**: shared mailboxes, grants, access log. Migration 0063.
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
