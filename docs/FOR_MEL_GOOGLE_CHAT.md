# For Mel: Google Chat for the ACMI Hub

Kellen turned on the Google Chat API and asked for Chat inside the hub: each person reads and answers their own Google Chat
(spaces, group chats, direct messages) in the hub, through their own Google sign-in, like Mail. The hub never reads anyone
else's chats. This is the Google-side work; it is the same project and the same sign-in client the hub already uses for
Gmail and Calendar. Google's own pages were read to write this; the first real "Turn on Chat" is what proves each step.

## In the Google Cloud project `sincere-passage-511621-j3` (the one with the hub's OAuth client)
1. **APIs & Services > Library:** make sure the **Google Chat API** is enabled in *this* project. (It has to be the project
   that owns the hub's OAuth client; if Kellen enabled it in the other "ACMI Properties Web App" project, enable it here.)
2. **APIs & Services > OAuth consent screen > Data access (scopes):** add these four, then save:
   - `https://www.googleapis.com/auth/chat.spaces.readonly`
   - `https://www.googleapis.com/auth/chat.memberships.readonly`
   - `https://www.googleapis.com/auth/chat.messages` (read and send; Google calls it "restricted", like Gmail's)
   - `https://www.googleapis.com/auth/chat.users.readstate` (which conversations are read, for the unread markers)
3. Check the consent screen's **User type is Internal**. Restricted permissions need Google's review for an External app;
   an Internal one is exempt (the hub's Gmail access relies on the same thing, see `docs/EMAIL_CLIENT.md`).
4. **APIs & Services > Google Chat API > Configuration** (needed for people to *send* from the hub; reading works without
   it): App name `ACMI Hub`, Avatar URL `https://hub.acmiproperties.com/static/crm/icons/icon-light-512.png`, Description
   `Chat from the ACMI Hub`. Leave **Interactive features** off. If Google insists on a Visibility choice, pick the whole
   organization if offered, otherwise add the teammates who will use Chat. Save. (Chat shows the app name beside messages
   sent this way.)

## In the Google Workspace Admin console (only if third-party access is restricted)
- **Security > Access and data control > API controls:** if the hub's client is already **Trusted** (it is for Gmail), nothing
  to do. If apps are restricted to an approved list, allow Google Chat for it.
- Google Chat should already be on for the organization (it is, if the team uses Chat today).

## What happens next, and what the hub says if a step is missing
Tell Kellen when 1 to 4 are done. Each person opens **Chat** in the hub and clicks **Turn on Chat**; Google shows its
permission screen once, listing the four Chat permissions. Nothing is read from anyone's Chat until they do.
- Google says the app is blocked or the permissions aren't allowed: step 2 or 3, or the Admin console.
- The hub says "Google Chat isn't switched on for the hub's Google project yet": step 1 (the wrong project, or not enabled).
- The hub says "Google needs the Chat API's Configuration page filled in": step 4. People can read before it's done.
- The hub says "The hub doesn't have the Chat permission yet": that person turns Chat off and on again.

No new secrets, no billing change, no service account.
