# Chat in the hub: your Google Chat, read and answered from the hub (built 2026-10-29; Google Chat's own features added the same night, branch `chat-parity`)

Kellen (2026-10-29, after turning on the Google Chat API): "build chat features into the hub. I'd either want it floating over
the page, as a sidebar, or like Gmail where it's at the bottom." All three are there, from one build: a pinned **Chat** box
and conversation boxes along the bottom of every page (Gmail's way; they float over the page), the same Chat page in the
split view's side (◫, the sidebar), and the Chat page on its own.

**What it's for:** talking with coworkers, since the team is remote. Chat is deliberately not tied to the CRM: nothing in it
links to a company, person or deal, and nothing from Chat goes into records or summaries (Kellen, 2026-10-29: "chats dont need
to be applied to any company").

Then (same night): "I want all the same functionality as Google chat does in their web app page. So quote text, emoji response
to a message, attachments." That is the section **Like Google Chat's own web app** below.

## What it does

- **Turn on Chat** (Chat in the top bar, once per person). Google shows its permission screen once; nothing is read from
  anyone's Chat before that, and nobody (Managers included) can see anyone else's conversations: every call goes through the
  signed-in person's own Google permission. Turn off stops the hub using it (Google keeps one permission for the hub, so only
  Turn off Mail removes the whole grant).
- **The list**: spaces (#), group chats (👥) and direct messages (👤), named the way Chat names them (a direct message by the
  other person, a group chat by the others), newest first, **unread ones in bold with a dot** (from Chat's own read state, so
  what you read in Google Chat is read here), a search box, refreshing every 20 seconds. Conversations quiet for 14 days never
  show as unread.
- **A conversation**: messages with avatars (the person's Google picture when Chat gives one), day separators, grouping by
  person, Chat's formatting (*bold*, _italic_, ~strike~, `code`, links, @mentions), thread replies indented. Enter sends,
  Shift+Enter is a new line; ↩ on a message answers in its thread; "Earlier messages" loads more; new messages arrive within
  about 6 seconds while it's open; 🔔 / 🔕 mutes its alerts; "Google Chat ↗" opens it in Chat. Opening a conversation marks it read.
- **In the top bar and at the bottom of every page (the dock, `MAIL_DOCK.md`)**: a 💬 button in the top bar, with the number of
  unread conversations, opens the Chat list in a panel under it (a click anywhere else closes it). Click a conversation to open
  it in a box at the bottom of the page (up to 4 boxes with the mail ones; they fold to a title bar, a folded one with
  something new shows a ● in its title, and they come back on the next page like the mail boxes). The unread count also sits
  beside Chat in the top bar. A phone gets one full-screen box. (The list was a pinned box at the bottom until 2026-11-03,
  and for a few days a 💬 button that opened it full screen on a phone; Kellen, 2026-10-30: "Chat is blocking the view".)
- **Alerts**: a new message from someone else (not yours, not in a muted conversation, not from before Chat was turned on)
  becomes a notification of kind "New chat message": the bell, a toast that opens that conversation's box, and a browser
  notification (Notification settings has its own switches). While the hub is open the dock's poll makes them within about 20
  seconds; with it closed the 15-minute sweep does (`notify.sweep_everyone`). The alert text holds the first 100 characters of
  the message (without Chat's markup), so mute a busy space or switch the kind off.

## Seen by (2026-11-05)

Under your latest message the conversation shows **✓ Delivered** or **✓✓ Seen by Simon, Dave**. Google's API gives each
person only their *own* read time, so the hub asks each teammate who turned Chat on in the hub (with the read-state
permission) for theirs (`service.seen_by`, `/chat/seen/`, every 15 seconds while the conversation is on screen, each
read time kept 20 seconds). That counts reading in Google Chat itself, not only in the hub. People without a hub
account or Chat turned on are not counted and never shown as "not seen"; with nobody countable no line shows. Spaces
of more than 12 people aren't asked about. Not checked against real Google (the fake Chat in the tests stands in).

## Like Google Chat's own web app

Hover a message for its buttons: **☺ react, ↩ reply in the thread, ❝ quote, ✎ edit, 🗑 delete** (the last two only on your own).

- **Emoji reactions.** ☺ opens a picker (your six quick ones, search by name, the usual groups, the ones you used lately);
  an emoji on a message shows as a chip with its count, yours highlighted; click a chip to add yours or take it away. Rest the
  pointer on a chip to see who gave it. Reactions others give show within about half a minute without reloading. Custom emoji
  from the organization show but can't be added from the hub.
- **Quote in reply.** ❝ puts the message above the box you write in; the reply carries Chat's own quote (shown above it on
  every device). Chat only allows a quote inside one thread, so quoting a thread reply also answers in that thread. If Chat
  won't take its own kind of quote for a person's account, the hub quotes in words (`↪ Name: “…”`) so the reply still says what
  it answers, and says nothing more about it.
- **Files.** 📎, dropping files on the conversation, or pasting a picture adds them to the message (up to ten, each up to
  25 MB: `ACMI_CHAT_MAX_UPLOAD_MB`; Chat itself takes 200 MB but Cloud Run takes 32 MiB in one request). Each goes to Chat as soon
  as you pick it, with a progress mark. Pictures show in the page; everything else is a card that downloads. Files from Drive
  show as links. Everything passes through the hub as the signed-in person (`/chat/file/`): only plain pictures are ever shown
  in the page, anything else (a web page, an SVG) is only ever a download.
- **Edit and delete your own messages.** ✎ edits in place (Enter saves, Esc cancels; a message made with mentions keeps them);
  an edited message says "(edited)". 🗑 takes it back; a conversation shows "This message was deleted." as Chat does, and
  deleting the start of a thread asks first because the replies go too.
- **@mentions and :emoji:.** Type `@` and a name (or `@all` in a space); `:tada` offers 🎉. The buttons under the box write
  Chat's markup around what's selected: **B**, *I*, ~~S~~, `</>`, bullets, 🔗 links, ☺ emoji (Ctrl/⌘ B and I work too).
- **Start a conversation.** ＋ beside the search box: tick one person for a direct message, several for a group chat, add a
  name to make a space (type an address to add someone who isn't listed). A direct message that already exists just opens.
  Starting a *new* one needs one more Google permission, which the person gives once from the same dialog ("Allow starting
  chats"); everything else works without it.
- **Search messages.** Type in the search box: the list narrows to matching conversations as you type; Enter (or the bar under
  the box) searches the text of every message you can see in Chat (Chat's own search, so it leaves out muted spaces and
  messages from Chat apps). A result opens its conversation at that message, with a little before it.
- **Not possible through Google's API, so not in the hub:** typing and "who's online" marks, calls and huddles, the GIF and
  Drive pickers, stickers, pinned and starred conversations, adding someone to an existing space, space settings, creating
  custom emoji. Forwarding a message to another conversation is possible in the API and not built yet.

## How it's built

- `crm/chat/connect.py`: the switch and the permissions (`CHAT_SCOPES`: spaces and memberships read-only, messages (which also
  covers reactions, edits, deletes, files and search), read state, and `chat.spaces.create` for starting conversations), the
  same allauth connect flow as Mail; the token stays in `SocialToken`. `ChatConnection` (migration `0074_chat_connection`,
  `0076_chatconnection_can_create`) holds the switch, `alerted_through` and whether Google gave the read-state and create
  permissions (people can untick single permissions: without read state there are just no unread markers, without create
  starting a brand-new conversation says so).
- `crm/chat/api.py`: the Chat API client (`ChatClient`), retrying when Google is busy, turning Google's errors into plain words
  (API not enabled, permission missing, "Configuration page" missing, busy). Names that reach a URL (`space_name`,
  `message_name`, `reaction_name`, `media_name`) and emoji (`emoji_ok`) are checked first. Text goes to Google with
  `markupSyntax: MARKUP_SYNTAX_CHAT` and is read back the same way: a Google project made lately reads text as Markdown unless
  told otherwise. Uploads are a `multipart/related` POST to `upload/v1/spaces/…/attachments:upload`.
- `crm/chat/service.py`: the logic: members and names (cached an hour), the overview with unread and muted marks (cached 15
  seconds, so the dock, a box and the page ask Google once), loading, posting (quote, files, the quote in words), editing,
  deleting, reactions (the emoji the person gave are asked once a minute per message that has any), uploading, starting
  conversations, searching, alerts, and the "re-read the newest 40" that catches what others changed.
- `crm/chat/render.py`: everything from Chat is HTML-escaped first, then links (http/https only), mentions and formatting are
  built; times, grouping, day separators; reactions, quotes, files and who may change a message. `plain_text` is a message
  without Chat's markup (for a quote's line, an alert, a search hit).
- `crm/chat/views.py`, templates `chat.html`, `chat_window.html`, `chat_list.html` and the `chat_*` partials, `static/crm/chat.js`
  (one script for the page, the list and a conversation, with the emoji picker), `static/crm/emoji.js` (the picker's emoji and
  the words they're found by), styles at the end of `app.css`; `dock.js` knows two more kinds of box (`chat`, and the pinned
  `chats`), polls `/chat/poll/`, opens a box at a found message, and the service worker's `hub-window` message opens a chat box too.
- Nothing from Chat is ever written to the page as HTML by the script; it builds the rest from text.
- 123 tests: `crm/tests/test_chat.py` (64) and `crm/tests/test_chat_parity.py` (59) with a fake Chat (`FakeChat`) that
  answers reactions, edits, deletes, uploads, downloads, new conversations and search as Google's pages describe them.

## What Google needs (Mel): `docs/FOR_MEL_GOOGLE_CHAT.md`

The API on in the hub's project, five scopes on the consent screen (Internal), the Chat API's Configuration page filled in (for
sending), and the Admin console letting the hub's client use Chat. Google's docs were read for this (and again for the new
features: reactions, quoting, uploads, search and starting conversations); **the first real "Turn on Chat" is what proves it**:
nothing here has run against real Google Chat.

## Checked, and not checked

Checked in the in-app browser on a scratch database with a fake Gmail and a fake Chat (never the real ones): the list and its
unread markers, opening and sending, formatting, thread replies, earlier messages, live messages arriving, the pinned box and
conversation boxes at the bottom, the toast opening a box, the sidebar in the split view, dark mode and a 375 px phone; and for
the new features: the picker and a reaction added, taken away and joined, the who-gave-it tooltip, a reaction by someone else
appearing by itself, quoting and the quote showing, a picture and a file uploaded, sent and shown, editing (saved and
cancelled), deleting, @mention and :emoji: suggestions, the formatting buttons, starting a direct message that exists, one that
needs the permission, a new one and a named space, searching and opening a result at its message, all of it in a dock box and
the picker inside one, and on a phone. Found and fixed there: the new-chat dialog took the style of the "New messages" pill
(same class name).

**Not checked:** anything against real Google (the connect screen, the real read-state, send, reaction, upload, quote and
search calls, the real shapes of direct-message and group-chat names, whether Chat takes its own quote for a person's account,
whether a project's text defaults behave as the pages say), Safari, Firefox, a real phone, a real browser notification click, a
file over a few MB, dropping files from a real desktop.
Google's docs leave things to see for real: whether direct messages and group chats are listed once they have a message (they
should be), whether a read time equal to "now" is accepted (the hub marks read with the current time), whether Chat's quote
works from a person's account (the hub falls back to words if not), and how `users/me` behaves in a reaction filter (the hub
uses the person's own id from the member list instead).

## Not built yet

Forwarding a message to another conversation (the API has it), adding or removing people in an existing space, renaming a space,
leaving one, picking a custom emoji (they show and can be seen but not added), a Chat alert when the hub is closed sooner than
the 15-minute sweep, live push with no polling (Workspace Events on the Pub/Sub topic Gmail push uses), a bigger-than-25-MB file
(it would need an upload straight to Google, not through the hub). Not wanted: tying Chat to companies, deals or campuses.
