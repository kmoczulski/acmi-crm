# E-signature and Google Chat in the hub: what's possible (asked 2026-10-29; nothing built yet)

Kellen: "could we have our own docusign management? What about adding google chat? Is it possible to build in google
chat?" Both fit the hub; neither is started. The mail dock (`MAIL_DOCK.md`) is built so either can be another kind of box.

## E-signature ("our own DocuSign")

Three levels, cheapest first; each builds on the one before.

1. **Track and file by email (no provider API; about 2 days).** The hub already reads Gmail. The provider's "Completed"
   email carries the signed PDF: match it to the contract (counterparty, subject, thread), attach it as the executed copy,
   set the contract Signed with its date, and let the existing Claude date extraction feed the critical dates. "Out for
   signature" is a status someone sets (or the "please sign" email sets). Works with whichever provider the other side uses.
2. **Send and track through a provider's API (about 1 to 2 weeks).** A "Send for signature" button on a contract: pick the
   signers (CRM people, ACMI users), the order, where the signature goes (anchor text in the document), and the provider
   emails them. The contract shows each signer's status, can remind or void, and files the executed PDF and the
   certificate when it completes. Needs an account with API access (DocuSign's developer sandbox to build with, then its
   go-live review), an integration key and either a service user (JWT) or each person's own OAuth, and a webhook (DocuSign
   Connect) or polling. Secrets go in Secret Manager (Mel). The code can stay provider-neutral (Dropbox Sign and Adobe Sign
   have API tiers too).
3. **Our own signing engine (about 3 to 5 weeks; not recommended for leases, PSAs or loan documents).** Click-to-sign is
   valid in the US for most commercial contracts under ESIGN / UETA when done properly (consent, intent, the record kept,
   an audit trail), but lenders, title companies, government counterparties and capital partners usually expect DocuSign
   or Adobe, recorded instruments still need a notary or wet ink, and we would own the tamper-evidence (document hashes, a
   sealed PDF, a certificate of completion), identity checks and retention. Reasonable only for low-stakes internal paper
   (NDAs, vendor forms).

Recommendation: level 1 now; level 2 if ACMI keeps paying for a provider anyway.
**Ask Kellen:** does ACMI already have DocuSign (which plan, and does it include API access) or is the aim to stop paying
for it? Which documents go out for signature, and who signs for ACMI?

## Google Chat

**Update, same day:** Kellen turned the Chat API on and asked for chat inside the hub (list, read, send, as boxes at the
bottom, or in the sidebar). That is built: `docs/CHAT.md`; what Google needs: `docs/FOR_MEL_GOOGLE_CHAT.md`. What follows is
the first analysis, kept as the record.

Checked against Google's Chat API docs on 2026-10-29. Kellen's follow-up: "I want to pull the google chat" (not a chat
client), and the first estimate here ("1 to 2 weeks", for a full client) was too high for how this project moves.

- **Pull, read-only (recommended first; about one build session).** Like the Gmail timeline: each person turns Chat on with
  their own Google sign-in and the hub copies the spaces they are in, new messages arriving with the 15-minute mail sync.
  Needs only the Chat API switched on and three read-only permissions (`chat.spaces.readonly`, `chat.memberships.readonly`,
  `chat.messages.readonly`). Google's docs say read calls need no Chat app configuration page (that is for posting). The
  wait is Mel (`FOR_MEL_GOOGLE_CHAT.md`) and each person's one click.
  - `chat.messages.readonly` is a **restricted** scope (spaces and memberships are sensitive), the same class as Gmail's
    `gmail.modify`; an **Internal** consent screen keeps it out of Google's review, as `EMAIL_CLIENT.md` notes for Gmail.
    (An earlier note here said all Chat scopes are "sensitive"; that was wrong for messages.)
  - Who is who: with a person's own sign-in the API names people `users/<id>` and fills in display name and email only for
    people in the same space (or with prior contact), enough to match them to CRM people by email. Group chats and DMs are
    listed only after their first message. Quota: 3,000 message reads a minute for the whole project; ten people polling
    every 15 minutes is far below it.
  - Copies are per person and private to them. (This analysis also proposed linking a space to a company, deal or campus,
    like "File to…" for mail. Kellen ruled that out on 2026-10-29: "chats dont need to be applied to any company", it is just
    for talking with coworkers while the team is remote. Chat is built without any tie to the CRM.)
- **Alerts into Chat (a few hours; no per-person consent).** A space's incoming webhook: the hub posts "deal moved", "reply
  from X", "task due" into a space the team picks. One way only.
- **Full Chat inside the hub (two or three more sessions after the pull).** Send and reply, live updates, boxes in the mail
  dock, bell and browser alerts. Needs the Chat app configuration page and the write permission (`chat.messages`), so more
  of Mel's time, and live updates through the Workspace Events API on the Pub/Sub topic the Gmail push already uses.

**Needs (Mel or a Workspace admin; I don't enable APIs):** see `FOR_MEL_GOOGLE_CHAT.md`.
