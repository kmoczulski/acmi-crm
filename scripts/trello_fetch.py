"""Pull every Trello board Kellen can see, read-only, with its full history, into one JSON file per board under
`data/imports/trello/<date>/` (gitignored), the input for the boards import.

Each file holds the board (lists, labels, members, custom fields), every card (open and archived, with checklists,
attachments and custom field values), the board's checklists and every action back to the board's start: comments
with their author and date, card creations, moves, due dates. Workspace boards Kellen isn't a member of are included.
It uses the news-review app's Trello key and token from its `config.json`, sends them in the Authorization header
(never in a URL) and never prints them. GET requests only; nothing in Trello changes.

Run it from a terminal at the laptop (Claude's auto mode won't use another app's key):
Usage: .venv/Scripts/python -I scripts/trello_fetch.py [news_review/config.json] [out dir]
"""
import collections
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.trello.com/1"
ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Desktop" / "Prospecting" / "news_review" / "config.json"
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "data" / "imports" / "trello" / datetime.now().strftime("%Y-%m-%d")
PAUSE = 0.12  # seconds between requests; Trello allows 100 per 10 s per token

_cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
_HEADERS = {
    "Authorization": f'OAuth oauth_consumer_key="{_cfg["trello_api_key"]}", oauth_token="{_cfg["trello_api_token"]}"',
    "Accept": "application/json",
}
del _cfg
calls = 0


def get(path, **params):
    global calls
    url = f"{API}{path}?{urllib.parse.urlencode(params)}" if params else f"{API}{path}"
    last = "HTTP 429/5xx"
    for attempt in range(6):
        calls += 1
        time.sleep(PAUSE)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=90) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"GET {path} -> HTTP {e.code}") from None
        except urllib.error.URLError as e:
            last = f"network error ({type(e.reason).__name__})"
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"GET {path} kept failing: {last}")


def all_actions(board_id):
    """Every action on the board, newest first, paging back 1,000 at a time."""
    actions, before = [], None
    while True:
        params = {"filter": "all", "limit": 1000}
        if before:
            params["before"] = before
        page = get(f"/boards/{board_id}/actions", **params)
        actions.extend(page)
        if len(page) < 1000:
            return actions
        before = page[-1]["id"]


def summarize(board, cards, checklists, actions, last_activity):
    lists = board.get("lists", [])
    open_by_list = collections.Counter(c["idList"] for c in cards if not c["closed"])
    kinds = collections.Counter(a["type"] for a in actions)
    comments = [a for a in actions if a["type"] == "commentCard"]
    authors = collections.Counter((a.get("memberCreator") or {}).get("fullName", "?") for a in comments)
    actors = collections.Counter((a.get("memberCreator") or {}).get("fullName", "?") for a in actions)
    workspace = (board.get("organization") or {}).get("displayName", "no workspace")
    print(f"\n=== {board['name']} [{workspace}]{' (closed)' if board['closed'] else ''} | last activity {last_activity}")
    print(f"  lists: {sum(1 for l in lists if not l['closed'])} open, {sum(1 for l in lists if l['closed'])} archived")
    for l in sorted(lists, key=lambda l: l["pos"]):
        if not l["closed"]:
            print(f"    - {l['name']}: {open_by_list.get(l['id'], 0)} open cards")
    print(f"  cards: {sum(1 for c in cards if not c['closed'])} open, {sum(1 for c in cards if c['closed'])} archived")
    print(f"  labels: {[(l['name'] or '(no name)', l['color']) for l in board.get('labels', [])]}")
    print(f"  members: {[m['fullName'] for m in board.get('members', [])]}")
    print(f"  checklists: {len(checklists)} with {sum(len(c.get('checkItems', [])) for c in checklists)} items | "
          f"attachments: {sum(len(c.get('attachments', [])) for c in cards)} | "
          f"custom fields: {[f['name'] for f in board.get('customFields', [])]}")
    print(f"  actions: {len(actions)} {dict(kinds.most_common(8))}")
    if actions:
        print(f"  history: {actions[-1]['date'][:10]} to {actions[0]['date'][:10]}")
    print(f"  comments: {len(comments)} by {dict(authors)}")
    print(f"  everyone with activity: {dict(actors)}")


def main():
    out = OUT if not OUT.exists() else OUT.with_name(f"{OUT.name}-{datetime.now():%H%M}")
    out.mkdir(parents=True)
    me = get("/members/me", fields="fullName,username")
    orgs = get("/members/me/organizations", fields="displayName,name")
    fields = "name,closed,idOrganization,dateLastActivity,shortLink,url"
    boards = {b["id"]: b for b in get("/members/me/boards", filter="all", fields=fields)}
    for org in orgs:
        for b in get(f"/organizations/{org['id']}/boards", filter="all", fields=fields):
            boards.setdefault(b["id"], b)
    print(f"Trello account: {me['fullName']} | workspaces: {[o['displayName'] for o in orgs]} | boards: {len(boards)}")

    index = {"fetched_at": datetime.now(timezone.utc).isoformat(), "member": me, "organizations": orgs, "boards": []}
    for bid, summary in sorted(boards.items(), key=lambda kv: kv[1].get("dateLastActivity") or "", reverse=True):
        board = get(f"/boards/{bid}", fields="all", labels="all", labels_limit=1000, lists="all", members="all",
                    memberships="all", customFields="true", organization="true")
        cards = get(f"/boards/{bid}/cards/all", fields="all", attachments="true", attachment_fields="all",
                    checklists="all", checkItemStates="true", customFieldItems="true", members="true",
                    member_fields="fullName,username", stickers="false")
        checklists = get(f"/boards/{bid}/checklists", checkItems="all", checkItem_fields="all")
        actions = all_actions(bid)
        slug = re.sub(r"[^a-z0-9]+", "-", board["name"].lower()).strip("-")[:40]
        name = f"{board['shortLink']}-{slug}.json"
        data = {"board": board, "cards": cards, "checklists": checklists, "actions": actions}
        (out / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        index["boards"].append({"id": bid, "name": board["name"], "file": name, "closed": board["closed"]})
        summarize(board, cards, checklists, actions, summary.get("dateLastActivity"))

    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nSaved {len(index['boards'])} boards to {out} ({calls} API calls).")


if __name__ == "__main__":
    main()
