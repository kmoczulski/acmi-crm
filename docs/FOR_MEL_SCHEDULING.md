# For Mel: scheduling meetings from the ACMI Hub

Kellen asked for scheduling in the hub: pick teammates, see when they are free, and have Google Calendar send the invitations
(`docs/SCHEDULING.md`). It uses the Google Calendar API the hub already uses for the calendar sync, in the same project and
with the same sign-in client, so there is very little to do. Each person's own Google permission is used; the hub never
reads or writes anyone else's calendar.

## In the Google Cloud project `sincere-passage-511621-j3`
1. **APIs & Services > OAuth consent screen > Data access (scopes):** add
   `https://www.googleapis.com/auth/calendar.events` ("View and edit events on all calendars") and save. The consent screen is
   Internal, so there is no Google review. (The hub's existing `calendar.readonly` permission is what answers "who is free";
   this one is only for putting the meeting on the person's own calendar.)
2. The **Google Calendar API** is already enabled (the calendar sync uses it). Nothing to add.

## In the Google Workspace Admin console
- **Apps > Google Workspace > Calendar > Sharing settings > Internal sharing (or "Primary calendars"):** teammates see each
  other's availability as long as this is **"Only free/busy information (hide event details)"** or more. If it is set to
  "Do not share", the hub shows "can't see their calendar" next to that person and plans around the people it can see.
- **Security > Access and data control > API controls:** the hub's client is already trusted (Gmail and Calendar work). If
  third-party access is limited to an approved list, nothing changes: this is the same Calendar access, with edit added.

## What happens next
Tell Kellen when step 1 is done. Each person then opens **Calendar > ＋ Schedule a meeting**, and the first time they press
**Allow scheduling**; Google shows one permission screen. Until they do, "Open in Google Calendar" still works for them.
- Google says the app is blocked or the permission isn't allowed: step 1.
- The hub says "can't see their calendar" for a teammate: the sharing setting above (or that person is not on Google Calendar).

No new secrets, no billing change, no service account, no new API to enable.
