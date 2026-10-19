"""Settings → Dropdown lists: rename, reorder, hide and (on open lists) add the options of any dropdown."""
import re
from collections import Counter

from django.contrib import messages
from django.db.models import Count
from django.http import Http404
from django.shortcuts import redirect, render

from . import choices
from .models import ChoiceOption
from .permissions import manager_required


@manager_required
def settings_home(request):
    return render(request, "crm/settings_home.html", {"lists": choices.lists()})


def _usage(key):
    """{value: how many records use it} across every field built from the list."""
    n = Counter()
    for f in choices._registry[key]["fields"]:
        for row in f.model.objects.values(f.name).annotate(c=Count("pk")):
            n[row[f.name]] += row["c"]
    return n


def _slug(label, taken, max_length):
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:max_length] or "option"
    value, i = base, 2
    while value in taken:
        tail = f"_{i}"
        value, i = base[:max_length - len(tail)] + tail, i + 1
    return value


@manager_required
def dropdown_edit(request, key):
    meta = choices.info(key)
    if meta is None:
        raise Http404("No such list")
    usage = _usage(key)
    if request.method == "POST":
        if request.POST.get("action") == "reset":
            removed = [o for o in ChoiceOption.objects.filter(list_key=key, added=True) if usage.get(o.value)]
            ChoiceOption.objects.filter(list_key=key).exclude(pk__in=[o.pk for o in removed]).delete()
            for o in removed:  # options the team added that records still use stay, at the end
                o.position = None
                o.save()
            messages.success(request, f"{meta['title']}: back to the built-in options."
                             + (" Added options still in use were kept." if removed else ""))
            return redirect("crm:dropdown_edit", key=key)
        current = choices.options(key)
        order = [v for v in request.POST.getlist("order") if v in {c[0] for c in current}]
        order += [c[0] for c in current if c[0] not in order]
        existing = {o.value: o for o in ChoiceOption.objects.filter(list_key=key)}
        errors = []
        for pos, value in enumerate(order):
            o = existing.get(value) or ChoiceOption(list_key=key, value=value)
            added = value not in meta["defaults"]
            if added and request.POST.get(f"remove_{value}"):
                if usage.get(value):
                    errors.append(f"“{o.label or value}” is used on {usage[value]} record{'s' if usage[value] != 1 else ''}: hide it instead.")
                elif o.pk:
                    o.delete()
                continue
            label = (request.POST.get(f"label_{value}") or "").strip()[:200]
            o.label = "" if not added and label == meta["defaults"].get(value) else (label or o.label)
            o.hidden = bool(request.POST.get(f"hidden_{value}"))
            o.position, o.added, o.updated_by = pos, added, request.user
            o.save()
        new = (request.POST.get("new_label") or "").strip()[:200]
        if new:
            if not meta["open"]:
                errors.append("New options can't be added to this list: the app's logic depends on its values.")
            elif new.lower() in {l.lower() for _, l, _, _ in choices.options(key)}:
                errors.append(f"“{new}” is already on the list.")
            else:
                taken = {v for v, _, _, _ in choices.options(key)}
                ChoiceOption.objects.create(list_key=key, value=_slug(new, taken, min(meta["max_length"], 40)), label=new,
                                            position=len(order), added=True, updated_by=request.user)
        for e in errors:
            messages.error(request, e)
        if not errors:
            messages.success(request, f"{meta['title']} saved.")
        return redirect("crm:dropdown_edit", key=key)
    rows = [{"value": v, "label": l, "hidden": h, "added": a, "default": meta["defaults"].get(v, ""),
             "used": usage.get(v, 0)} for v, l, h, a in choices.options(key)]
    return render(request, "crm/settings_dropdown.html", {"meta": meta, "rows": rows,
                                                          "edited": ChoiceOption.objects.filter(list_key=key).exists()})
