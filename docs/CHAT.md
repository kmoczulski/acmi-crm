# Chat in the hub: your Google Chat, read and answered from the hub (built 2026-10-29, branch `mail-dock`, not merged)

Kellen (2026-10-29, after turning on the Google Chat API): "build chat features into the hub. I'd either want it floating over
the page, as a sidebar, or like Gmail where it's at the bottom." All three are there, from one build: a pinned **Chat** box
and conversation boxes along the bottom of every page (Gmail's way; they float over the page), the same Chat page in the
split view's side (◫, the sidebar), and the Chat page on its own.

## What it does

- **Turn on Chat** (Chat in the top bar, once per person). Google shows its permission screen once; nothing is read from
  anyone's Chat before that, and nobody (Managers included) can see anyone else's conversations: every call goes through the
  signed-in person's own Google permission. Turn off stops the hub using it (Google keeps one permission for the hub, so only
  Turn off Mail removes the whole grant).
- **The list**: spaces (#), group chats (👥) and direct messages (👤), named the way Chat names them (a direct message by the
  other person, a group chat by the others), newest first, **unread ones in bold with a dot** (from Chat's own read state, so
  what you read in Google Chat is read here), a search box, refreshing every 20 seconds. Conversations quiet for 14 days never
  show as unread.
- **A conversation**: messages with avatars, day separators, grouping by person, Chat's formatting (*bold*, _italic_, ~strike~,
  `code`, links, @mentions, attachments as links), thread replies indented. Enter sends, Shift+Enter is a new line; ↩ on a
  message answers in its thread; "Earlier messages" loads more; new messages arrive within about 6 seconds while it's open;
  🔔 / 🔕 mutes its alerts; "Google Chat ↗" opens it in Chat. People in it who are in the CRM show as links to their person
  and company. Opening a conversation marks it read.
- **At the bottom of every page (the dock, `MAIL_DOCK.md`)**: a pinned **Chat (3)** box at the right: click it to see the list,
  click a conversation to open it in a box beside it (up to 4 boxes with the mail ones; they fold to a title bar, a folded one
  with something new shows a ● in its title, and they come back on the next page like the mail boxes). On the Chat page itself
  the pinned box hides (the page is the list) and the unread count sits beside Chat in the top bar. A phone gets one
  full-screen box.
- **Alerts**: a new message from someone else (not yours, not in a muted conversation, not from before Chat was turned on)
  becomes a notification of kind "New chat message": the bell, a toast that opens that conversation's box, and a browser
  notification (Notification settings has its own switches). While the hub is open the dock's poll makes them within about 20
  seconds; with it closed the 15-minute sweep does (`notify.sweep_everyone`). The alert text holds the first 100 characters of
  the message, so mute a busy space or switch the kind off.

## How it's built

- `crm/chat/connect.py`: the switch and the permissions (`CHAT_SCOPES`: spaces and memberships read-only, messages read and
  send, read state), the same allauth connect flow as Mail; the token stays in `SocialToken`. `ChatConnection` (migration
  `0074_chat_connection`, which also adds the Notification kind) holds the switch, `alerted_through` and whether Google gave
  the read-state permission (people can untick single permissions; without it there are just no unread markers).
- `crm/chat/api.py`: the Chat API client (`ChatClient`), retrying when Google is busy, turning Google's errors into plain words
  (API not enabled, permission missing, "Configuration page" missing, busy). `space_name` is the only way an id reaches a URL.
- `crm/chat/service.py`: the logic: members and names (cached an hour), the overview with unread and muted marks (cached 15
  seconds, so the dock, a box and the page ask Google once), loading, posting, mark read, mute (`UserPrefs chat_muted`), alerts.
- `crm/chat/render.py`: everything from Chat is HTML-escaped first, then links (http/https only), mentions and formatting are
  built; times, grouping, day separators.
- `crm/chat/views.py`, templates `chat.html`, `chat_window.html`, `chat_list.html` and the `chat_*` partials, `static/crm/chat.js`
  (one script for the page, the list and a conversation), styles at the end of `app.css`; `dock.js` knows two more kinds of box
  (`chat`, and the pinned `chats`), polls `/chat/poll/`, and the service worker's `hub-window` message opens a chat box too.
- 64 tests in `crm/tests/test_chat.py` with a fake Chat server (`FakeChat`); the whole suite (1,023 tests) passes.

## What Google needs (Mel): `docs/FOR_MEL_GOOGLE_CHAT.md`

The API on in the hub's project, four scopes on the consent screen (Internal), the Chat API's Configuration page filled in (for
sending), and the Admin console letting the hub's client use Chat. Google's docs were read for this; **the first real "Turn on
Chat" is what proves it**: nothing here has run against real Google Chat.

## Checked, and not checked

Checked in the in-app browser on a scratch database with a fake Gmail and a fake Chat (never the real ones): the list and its
unread markers, opening and sending, formatting, thread replies, earlier messages, live messages arriving, the pinned box and
conversation boxes at the bottom (several at once, folding, the ● on a folded one), the toast opening a box, the sidebar in the
split view, dark mode, and a 375 px phone. Found and fixed there: a conversation left by clicking another kept polling and
marking itself read.

**Not checked:** anything against real Google (the connect screen, the real read-state and send calls, the real shapes of
direct-message and group-chat names), Safari, Firefox, a real phone, a real browser notification click.
Google's docs leave two things to see for real: whether direct messages and group chats are listed once they have a message
(they should be), and whether a read time equal to "now" is accepted (the hub marks read with the current time).

## Not built yet

Starting a new conversation (a direct message with a teammate or a new space), sending attachments, emoji reactions, editing or
deleting a message, files from Chat into the CRM, filing a conversation to a company, deal or campus (and Claude's campus summary
reading it), live push with no polling (Workspace Events on the Pub/Sub topic Gmail push uses), a Chat alert when the hub is
closed sooner than the 15-minute sweep.
