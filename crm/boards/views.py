"""Boards: My boards, a board of lists and cards, the card dialog and every action on them.

Forms in the card dialog post normally and come back to the board with `?card=<id>`, which reopens the dialog, so a
slow network or a refused action never leaves the page in a half state. Moves are the one fetch (board.js).
"""
from datetime import date

from django.contrib import messages
from django.db.models import Count, Prefetch, Q
from django.http import FileResponse, Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .. import notify
from ..forms import active_users
from ..models import (
    BOARD_COLOURS, LABEL_COLOURS, Board, BoardLabel, BoardList, Card, CardAttachment, Checklist, ChecklistItem,
)
from ..permissions import is_manager, manager_required, refuse
from . import logic
from .forms import BoardForm, BoardSettingsForm, MembersForm

CARD_SELECT = ("list", "opportunity__company", "company", "site", "contract", "firm", "approval", "capital_source",
               "meeting", "task")


def _back(request, fallback):
    nxt = request.POST.get("next") or request.GET.get("next")
    ok = nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}) and nxt.startswith("/")
    return nxt if ok else fallback


def _card_url(card):
    return card.get_absolute_url()


def _fetch(request):
    return request.headers.get("x-requested-with") == "fetch"


# ---------------------------------------------------------------------------
# Boards
# ---------------------------------------------------------------------------


def board_index(request):
    show_archived = request.GET.get("archived") == "1"
    boards = (Board.objects.filter(archived=show_archived).select_related("campus", "project")
              .prefetch_related("members")
              .annotate(n_cards=Count("lists__cards", filter=Q(lists__cards__archived=False, lists__archived=False), distinct=True),
                        n_due=Count("lists__cards", filter=Q(lists__cards__archived=False, lists__archived=False,
                                                              lists__cards__done=False,
                                                              lists__cards__due__lte=timezone.localdate() + timezone.timedelta(days=7)),
                                    distinct=True)))
    mine = [b for b in boards if request.user in b.members.all()]
    others = [b for b in boards if b not in mine]
    form = BoardForm(initial={"campus": None})
    lists_by_board = {}
    for row in BoardList.objects.filter(archived=False, board__archived=False).order_by("position", "id").values("board_id", "id", "name"):
        lists_by_board.setdefault(str(row["board_id"]), []).append({"id": row["id"], "name": row["name"]})
    return render(request, "crm/boards.html", {
        "mine": mine, "others": others, "show_archived": show_archived, "form": form,
        "new": request.GET.get("new", ""), "lists_json": lists_by_board, "all_boards": [b for b in boards if not b.archived],
    })


@require_POST
def board_create(request):
    form = BoardForm(request.POST)
    if not form.is_valid():
        messages.error(request, "A board needs a name.")
        return redirect(reverse("crm:boards") + "?new=board")
    d = form.cleaned_data
    board = logic.create_board(d["name"], request.user, kind=d["kind"], template=d["template"] or "blank", campus=d["campus"],
                               project=d["project"], colour=d["colour"], description=d["description"])
    messages.success(request, f"Board “{board.name}” created.")
    return redirect(board)


@require_POST
def quick_card(request):
    """+ New › Card: a title on a board and list of your choice."""
    lst = get_object_or_404(BoardList.objects.select_related("board"), pk=request.POST.get("list"), archived=False)
    title = request.POST.get("title", "").strip()
    if not title:
        messages.error(request, "A card needs a title.")
        return redirect(reverse("crm:boards") + "?new=card")
    card = logic.add_card(lst, title, request.user)
    card.members.add(request.user)
    return redirect(_card_url(card))


def _filtered_cards(request, board):
    """Open cards on the board after the filter bar: text, member (or mine), label, due."""
    cards = (Card.objects.filter(list__board=board, archived=False)
             .select_related(*CARD_SELECT).prefetch_related("labels", "members")
             .annotate(n_comments=Count("comments", distinct=True), n_attachments=Count("attachments", distinct=True),
                       n_items=Count("checklists__items", distinct=True),
                       n_done=Count("checklists__items", filter=Q(checklists__items__done=True), distinct=True)))
    g = request.GET
    if q := g.get("q", "").strip():
        cards = cards.filter(Q(title__icontains=q) | Q(description__icontains=q))
    member = g.get("member", "")
    if member == "me" or g.get("mine") == "1":
        cards = cards.filter(members=request.user)
    elif member.isdigit():
        cards = cards.filter(members__pk=int(member))
    if (label := g.get("label", "")).isdigit():
        cards = cards.filter(labels__pk=int(label))
    today = timezone.localdate()
    due = g.get("due", "")
    if due == "late":
        cards = cards.filter(done=False, due__lt=today)
    elif due == "week":
        cards = cards.filter(done=False, due__gte=today, due__lte=today + timezone.timedelta(days=7))
    elif due == "none":
        cards = cards.filter(due__isnull=True)
    elif due == "done":
        cards = cards.filter(done=True)
    return cards.distinct()


def board_detail(request, pk):
    board = get_object_or_404(Board.objects.select_related("campus", "project").prefetch_related("members", "labels"), pk=pk)
    lists = list(board.lists.filter(archived=False))
    by_list = {}
    for c in _filtered_cards(request, board).order_by("position", "id"):
        by_list.setdefault(c.list_id, []).append(c)
    for l in lists:
        l.open_cards = by_list.get(l.pk, [])
    filtered = any(request.GET.get(k) for k in ("q", "member", "label", "due", "mine"))
    open_card = request.GET.get("card", "")
    return render(request, "crm/board.html", {
        "board": board, "lists": lists, "page": f"board:{board.pk}", "filtered": filtered,
        "users": active_users(), "label_colours": LABEL_COLOURS, "board_colours": BOARD_COLOURS,
        "open_card": open_card if open_card.isdigit() else "",
        "settings_form": BoardSettingsForm(instance=board), "members_form": MembersForm(initial={"members": board.members.all()}),
        "archived_lists": board.lists.filter(archived=True),
        "archived_cards": Card.objects.filter(list__board=board, archived=True).select_related("list").order_by("-updated_at")[:50],
        "can_manage": is_manager(request.user),
    })


@require_POST
def board_edit(request, pk):
    board = get_object_or_404(Board, pk=pk)
    action = request.POST.get("action", "settings")
    if action == "settings":
        form = BoardSettingsForm(request.POST, instance=board)
        if form.is_valid():
            form.save()
            messages.success(request, "Board saved.")
        else:
            messages.error(request, "Fix the board details: " + "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items()))
    elif action == "members":
        form = MembersForm(request.POST)
        if form.is_valid():
            board.members.set(form.cleaned_data["members"])
            messages.success(request, "Members saved.")
    elif action == "archive":
        if not is_manager(request.user):
            return refuse(request)
        board.archived = True
        board.save(update_fields=["archived"])
        messages.success(request, f"Archived “{board.name}”. Find it under Archived boards.")
        return redirect("crm:boards")
    elif action == "unarchive":
        board.archived = False
        board.save(update_fields=["archived"])
        messages.success(request, f"“{board.name}” is back.")
    elif action == "delete":
        if not is_manager(request.user):
            return refuse(request)
        name = board.name
        board.delete()
        messages.success(request, f"Deleted board “{name}” and its cards.")
        return redirect("crm:boards")
    elif action == "label_add":
        name = request.POST.get("name", "").strip()[:60]
        colour = request.POST.get("colour", "blue")
        if name:
            last = board.labels.order_by("-position").first()
            BoardLabel.objects.create(board=board, name=name, colour=colour if colour in dict(LABEL_COLOURS) else "blue",
                                      position=(last.position + 1) if last else 1)
    elif action == "label_edit":
        label = get_object_or_404(BoardLabel, pk=request.POST.get("label"), board=board)
        if request.POST.get("delete"):
            label.delete()
        else:
            label.name = request.POST.get("name", label.name).strip()[:60] or label.name
            colour = request.POST.get("colour", label.colour)
            label.colour = colour if colour in dict(LABEL_COLOURS) else label.colour
            label.save()
    elif action == "list_add":
        name = request.POST.get("name", "").strip()
        if name:
            lst = logic.add_list(board, name)
            if _fetch(request):
                return render(request, "crm/partials/board_list.html", {"l": lst, "board": board, "users": active_users()})
    return redirect(_back(request, board.get_absolute_url()))


@require_POST
def list_edit(request, pk):
    lst = get_object_or_404(BoardList.objects.select_related("board"), pk=pk)
    action = request.POST.get("action", "")
    if action == "rename":
        name = request.POST.get("name", "").strip()[:120]
        if name:
            lst.name = name
            lst.save(update_fields=["name"])
    elif action == "archive":
        lst.archived = True
        lst.save(update_fields=["archived"])
        messages.success(request, f"Archived the list “{lst.name}” and its cards. Board menu › Archived items brings it back.")
    elif action == "unarchive":
        lst.archived = False
        lst.save(update_fields=["archived"])
    elif action == "move":
        lists = list(lst.board.lists.filter(archived=False).order_by("position", "id"))
        index = next((i for i, x in enumerate(lists) if x.pk == lst.pk), 0)
        where = request.POST.get("to", "")
        index = index - 1 if where == "left" else index + 1 if where == "right" else int(where) if where.isdigit() else index
        logic.move_list(lst, max(0, index))
        if _fetch(request):
            return JsonResponse({"ok": True})
    elif action == "sort":
        logic.sort_list(lst, request.POST.get("by", ""))
    elif action == "move_cards":
        target = get_object_or_404(BoardList, pk=request.POST.get("to_list"), board=lst.board, archived=False)
        for c in list(lst.cards.filter(archived=False).order_by("position", "id")):
            logic.place(c, target, 10**6, request.user)
        messages.success(request, f"Moved every card to {target.name}.")
    return redirect(_back(request, lst.board.get_absolute_url()))


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------


@require_POST
def card_add(request, list_pk):
    lst = get_object_or_404(BoardList.objects.select_related("board"), pk=list_pk, archived=False)
    title = request.POST.get("title", "").strip()
    if not title:
        if _fetch(request):
            return HttpResponseBadRequest("A card needs a title.")
        messages.error(request, "A card needs a title.")
        return redirect(lst.board.get_absolute_url())
    card = logic.add_card(lst, title, request.user)
    if _fetch(request):
        card = Card.objects.select_related(*CARD_SELECT).annotate(
            n_comments=Count("comments", distinct=True), n_attachments=Count("attachments", distinct=True),
            n_items=Count("checklists__items", distinct=True),
            n_done=Count("checklists__items", filter=Q(checklists__items__done=True), distinct=True)).get(pk=card.pk)
        return render(request, "crm/partials/board_card.html", {"c": card})
    return redirect(_card_url(card))


def card_dialog(request, pk):
    """The card, as a fragment for the board's dialog (or a page on its own when opened directly)."""
    card = get_object_or_404(Card.objects.select_related(*CARD_SELECT, "list__board", "created_by")
                             .prefetch_related("labels", "members", "checklists__items__assignee", "attachments__added_by",
                                               "events__user"), pk=pk)
    board = card.list.board
    ctx = {
        "card": card, "board": board, "lists": board.lists.filter(archived=False), "labels": board.labels.all(),
        "users": active_users(), "facts": logic.record_facts(card), "record": card.record(),
        "comments": card.comments.select_related("author"), "comment_target": ("card", card.pk),
        "can_manage": is_manager(request.user), "label_colours": LABEL_COLOURS, "next": _card_url(card),
        "members": card.members.all(), "my_member": request.user in card.members.all(),
    }
    if _fetch(request) or request.GET.get("fragment"):
        return render(request, "crm/partials/card_dialog.html", ctx)
    return redirect(_card_url(card))


def _date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


@require_POST
def card_edit(request, pk):
    card = get_object_or_404(Card.objects.select_related("list__board"), pk=pk)
    board = card.list.board
    action = request.POST.get("action", "")
    user = request.user
    if action == "title":
        title = request.POST.get("title", "").strip()
        if not title:
            messages.error(request, "A card needs a title.")
        elif title != card.title:
            logic.log(card, user, "renamed", f"renamed this card from “{card.title[:80]}”")
            card.title = title[:300]
            card.save(update_fields=["title", "updated_at"])
    elif action == "description":
        card.description = request.POST.get("description", "").strip()
        card.save(update_fields=["description", "updated_at"])
    elif action == "dates":
        card.start = _date(request.POST.get("start", ""))
        card.due = _date(request.POST.get("due", ""))
        t = request.POST.get("due_time", "")
        card.due_time = t if (t and card.due) else None
        card.save(update_fields=["start", "due", "due_time", "updated_at"])
        logic.log(card, user, "due", f"set the due date to {card.due:%b %d, %Y}" if card.due else "removed the due date")
    elif action == "done":
        card.done = request.POST.get("done") == "1"
        card.done_at = timezone.now() if card.done else None
        card.save(update_fields=["done", "done_at", "updated_at"])
        logic.log(card, user, "done", "marked this card done" if card.done else "marked this card not done")
    elif action == "labels":
        ids = [int(x) for x in request.POST.getlist("label") if x.isdigit()]
        card.labels.set(board.labels.filter(pk__in=ids))
        logic.log(card, user, "label", "changed the labels: " + (", ".join(l.name for l in card.labels.all()) or "none"))
    elif action == "members":
        before = set(card.members.values_list("pk", flat=True))
        ids = [int(x) for x in request.POST.getlist("member") if x.isdigit()]
        chosen = active_users().filter(pk__in=ids)
        card.members.set(chosen)
        added = [u for u in chosen if u.pk not in before]
        notify.card_members_added(card, added, user)
        if added:
            logic.log(card, user, "member", "added " + ", ".join(u.get_full_name() or u.username for u in added))
    elif action == "join":
        card.members.add(user)
        logic.log(card, user, "member", "joined this card")
    elif action == "leave":
        card.members.remove(user)
        logic.log(card, user, "member", "left this card")
    elif action == "cover":
        colour = request.POST.get("cover", "")
        card.cover = colour if colour in dict(LABEL_COLOURS) else ""
        card.save(update_fields=["cover", "updated_at"])
    elif action == "link":
        field, rid = logic.link_from_url(request.POST.get("record_url", ""))
        if request.POST.get("record_field") in Card.RECORD_FIELDS and request.POST.get("record_id", "").isdigit():
            field, rid = request.POST["record_field"], int(request.POST["record_id"])
        try:
            if field:
                rec = logic.set_record(card, field, rid, user)
                if not card.title or request.POST.get("rename") == "1":
                    card.title = str(rec)[:300]
                    card.save(update_fields=["title"])
            else:
                messages.error(request, "Pick a record from the list.")
        except logic.RECORD_MODELS.get(field, Card).DoesNotExist:
            messages.error(request, "That record no longer exists.")
    elif action == "unlink":
        logic.set_record(card, None, None, user)
    elif action == "move":
        lst = get_object_or_404(BoardList, pk=request.POST.get("list"), board=board, archived=False)
        pos = request.POST.get("position", "")
        index = int(pos) - 1 if pos.isdigit() else 10**6
        logic.place(card, lst, index, user)
    elif action == "copy":
        new = logic.copy_card(card, user, request.POST.get("title", "").strip() or None)
        messages.success(request, f"Copied to “{new.title}”.")
        return redirect(_card_url(new))
    elif action == "archive":
        card.archived = True
        card.save(update_fields=["archived", "updated_at"])
        logic.log(card, user, "archived", "archived this card")
        messages.success(request, f"Archived “{card.title}”. Board menu › Archived items brings it back.")
        return redirect(board.get_absolute_url())
    elif action == "unarchive":
        card.archived = False
        card.save(update_fields=["archived", "updated_at"])
        logic.log(card, user, "archived", "sent this card back to the board")
    elif action == "delete":
        if not is_manager(request.user):
            return refuse(request)
        if not card.archived:
            messages.error(request, "Archive the card first; only archived cards can be deleted.")
            return redirect(_card_url(card))
        title = card.title
        card.delete()
        messages.success(request, f"Deleted “{title}”.")
        return redirect(board.get_absolute_url())
    else:
        return HttpResponseBadRequest("Unknown action")
    if _fetch(request):
        return JsonResponse({"ok": True})
    return redirect(_back(request, _card_url(card)))


@require_POST
def card_move(request, pk):
    """Drag and drop (and the keyboard move): list + index -> JSON."""
    card = get_object_or_404(Card.objects.select_related("list__board"), pk=pk, archived=False)
    lst = BoardList.objects.filter(pk=request.POST.get("list"), board=card.list.board, archived=False).first()
    if lst is None:
        return JsonResponse({"ok": False, "error": "That list is not on this board."}, status=400)
    try:
        index = int(request.POST.get("index", "0"))
    except ValueError:
        index = 0
    position = logic.place(card, lst, index, request.user)
    return JsonResponse({"ok": True, "list": lst.pk, "position": position})


@require_POST
def checklist_edit(request, pk):
    card = get_object_or_404(Card.objects.select_related("list__board"), pk=pk)
    action = request.POST.get("action", "")
    if action == "add":
        last = card.checklists.order_by("-position").first()
        Checklist.objects.create(card=card, name=request.POST.get("name", "").strip()[:120] or "Checklist",
                                 position=(last.position + 1) if last else 1)
    elif action == "delete":
        get_object_or_404(Checklist, pk=request.POST.get("checklist"), card=card).delete()
    elif action == "item_add":
        cl = get_object_or_404(Checklist, pk=request.POST.get("checklist"), card=card)
        text = request.POST.get("text", "").strip()
        if text:
            last = cl.items.order_by("-position").first()
            ChecklistItem.objects.create(checklist=cl, text=text[:300], position=(last.position + 1) if last else 1,
                                         due=_date(request.POST.get("due", "")))
    elif action == "item_toggle":
        item = get_object_or_404(ChecklistItem, pk=request.POST.get("item"), checklist__card=card)
        item.done = not item.done
        item.save(update_fields=["done"])
        if item.task_id:
            item.task.done = item.done
            item.task.done_at = timezone.now() if item.done else None
            item.task.save(update_fields=["done", "done_at"])
        if _fetch(request):
            return JsonResponse({"ok": True, "done": item.done})
    elif action == "item_delete":
        get_object_or_404(ChecklistItem, pk=request.POST.get("item"), checklist__card=card).delete()
    elif action == "item_assign":
        item = get_object_or_404(ChecklistItem, pk=request.POST.get("item"), checklist__card=card)
        who = request.POST.get("assignee", "")
        item.assignee = active_users().filter(pk=who).first() if who.isdigit() else None
        item.due = _date(request.POST.get("due", "")) if "due" in request.POST else item.due
        item.save(update_fields=["assignee", "due"])
    return redirect(_back(request, _card_url(card)))


@require_POST
def attachment_edit(request, pk):
    card = get_object_or_404(Card.objects.select_related("list__board"), pk=pk)
    action = request.POST.get("action", "add")
    if action == "add":
        url = request.POST.get("url", "").strip()
        f = request.FILES.get("file")
        if not url and not f:
            messages.error(request, "Add a link or choose a file.")
        else:
            name = request.POST.get("name", "").strip() or (f.name if f else url.rstrip("/").rsplit("/", 1)[-1][:100] or "Link")
            CardAttachment.objects.create(card=card, name=name[:300], url=url[:1000], file=f, added_by=request.user)
            logic.log(card, request.user, "attached", f"attached {name[:120]}")
    elif action == "delete":
        att = get_object_or_404(CardAttachment, pk=request.POST.get("attachment"), card=card)
        if att.added_by_id == request.user.pk or is_manager(request.user):
            att.delete()
        else:
            messages.error(request, "Only whoever added an attachment (or a Manager) can remove it.")
    return redirect(_back(request, _card_url(card)))


def attachment_file(request, pk):
    att = get_object_or_404(CardAttachment, pk=pk)
    if not att.file:
        raise Http404
    return FileResponse(att.file.open("rb"), as_attachment=False, filename=att.name)


@require_POST
def record_add_to_board(request):
    """"Add to board" on a record page: a card for this record in the first list of the chosen board."""
    field = request.POST.get("field", "")
    pk = request.POST.get("pk", "")
    if field not in Card.RECORD_FIELDS or not pk.isdigit():
        raise Http404
    record = get_object_or_404(logic.RECORD_MODELS[field], pk=int(pk))
    board = get_object_or_404(Board, pk=request.POST.get("board"), archived=False)
    lst = board.lists.filter(archived=False).order_by("position", "id").first()
    if lst is None:
        lst = logic.add_list(board, "To do")
    card = logic.add_card(lst, str(record), request.user, **{field: record})
    card.members.add(request.user)
    messages.success(request, f"Added to {board.name} › {lst.name}.")
    return redirect(_back(request, _card_url(card)))
