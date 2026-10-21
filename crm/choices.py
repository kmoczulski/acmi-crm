"""Editable dropdown lists (Settings → Dropdown lists).

Every dropdown in the app comes from a TextChoices class in crm/models.py. The defaults stay in code; a manager can
rename, reorder or hide options and add new ones. On lists that drive logic (stages, statuses) a new option says which
built-in option it works like (ChoiceOption.behaves_as): the stage orders, open / closed groups and probabilities
defined with Order, Group and Lookup below then treat it as that option.
Those edits live in ChoiceOption rows and apply everywhere the list is used: forms, `X.Status.choices`,
`member.label` and `obj.get_status_display()`.

How it hooks in (install(), from CrmConfig.ready):
- ChoicesType.choices (the metaclass property behind `X.Status.choices`, `.labels`, `.values`) and Choices.label
  consult the edits for the registered classes.
- Each model field built from a registered class gets a callable `choices`, so displays and validation see renamed
  and added options; its deconstruct() still reports the code defaults, so migrations never change.
- ModelForm fields for those model fields offer the visible options (a hidden option still shows on a record that
  already has it).
"""
import time

from django.db import models
from django.db.models import enums

TTL = 30  # seconds before another server process picks up an edit

# Lists only the system sets (sync states, activity kinds...): not shown in Settings.
SYSTEM = {
    "ObjectType", "Activity.Kind", "Activity.Source", "Activity.Context", "Notification.Kind", "MailFilter.Kind",
    "NewsScan.Status", "NewsItem.Kind", "NewsItem.Decision", "SendBatch.Status", "SendItem.Status",
    "ContractDocument.TextStatus", "ContractDate.Unit", "CampusReport.Status", "Repeat", "CapitalLog.Kind",
}
# Lists where the code only names a few values (or none): a new option needs nothing more. On every other list a new
# option picks the built-in option it works like.
OPEN = {
    "DealType", "AssetType", "OpportunityContact.Role", "Task.Label", "Contract.Kind", "ContractDate.Kind",
    "ApprovalDocument.Kind", "TeamMember.Role", "ChangeOrder.Reason", "BudgetLine.Group", "CampusUpdate.Workstream",
    "TermSheet.Kind",
}
TITLES = {
    "Priority": ("Contacts", "Priority"), "CompanyStatus": ("Contacts", "Company stage"),
    "CampusPlan": ("Contacts", "Campus of interest"), "OpportunityContact.Role": ("Tenants", "Contact role on a deal"),
    "OpportunityStage": ("Tenants", "Tenant deal stage"), "DealType": ("Tenants", "Deal type"),
    "AssetType": ("Tenants", "Asset type"), "Task.Label": ("Tasks", "Task label"),
    "CampusUpdate.Workstream": ("Campuses", "Campus update workstream"),
    "CampusUpdate.Waiting": ("Campuses", "Ball in court"), "Parcel.Status": ("Land", "Parcel review status"),
    "LandSite.Status": ("Land", "Site stage"), "Contract.Kind": ("Contracts", "Contract type"),
    "Contract.Status": ("Contracts", "Contract status"), "Contract.Decision": ("Contracts", "Feasibility decision"),
    "ContractDocument.Kind": ("Contracts", "Contract document type"),
    "ContractDocument.Sender": ("Contracts", "Draft sent by"), "ContractDate.Kind": ("Contracts", "Critical date type"),
    "DiligenceItem.Status": ("Land", "Diligence item status"), "ClosingItem.Side": ("Contracts", "Closing item party"),
    "ClosingItem.Status": ("Contracts", "Closing item status"), "Approval.Kind": ("Entitlements", "Approval type"),
    "Approval.Status": ("Entitlements", "Approval status"), "ApprovalStep.Kind": ("Entitlements", "Approval step"),
    "ApprovalDocument.Kind": ("Entitlements", "Approval document type"),
    "ApprovalCondition.Status": ("Entitlements", "Condition status"), "BuildMilestone.Phase": ("Build", "Milestone phase"),
    "TeamMember.Role": ("Build", "Project team role"), "TeamMember.Status": ("Build", "Team member status"),
    "Bid.Status": ("Build", "Bid status"), "BudgetLine.Group": ("Build", "Budget group"),
    "ChangeOrder.Status": ("Build", "Change order status"), "ChangeOrder.Reason": ("Build", "Change order reason"),
    "Draw.Status": ("Build", "Draw status"), "LandownerLetter.Method": ("Land", "Letter sent by"),
    "CapitalSource.Kind": ("Capital", "Capital source type"), "CapitalSource.Status": ("Capital", "Capital source stage"),
    "TermSheet.Kind": ("Capital", "Term sheet type"), "TermSheet.Status": ("Capital", "Term sheet status"),
    "Firm.Kind": ("Contacts", "Directory type"),
}
AREAS = ["Land", "Tenants", "Contracts", "Entitlements", "Campuses", "Build", "Capital", "Tasks", "Contacts", "Other"]

_registry = {}  # key -> {"cls", "defaults": [(value, label)], "fields": [model field], "max_length"}
_by_cls = {}  # TextChoices class -> key
_field_key = {}  # (model, field name) -> key, for the fields built from a registered class
_cache = {"at": 0.0, "edits": None}
_original_choices = enums.ChoicesType.choices
_original_label = enums.Choices.__dict__["label"]


def key_of(cls):
    return cls.__qualname__


def clear():
    _cache["at"], _cache["edits"] = 0.0, None


def _edits():
    """{key: {value: ChoiceOption}} from the database, cached for TTL seconds; {} before the table exists."""
    now = time.monotonic()
    if _cache["edits"] is not None and now - _cache["at"] < TTL:
        return _cache["edits"]
    edits = {}
    try:
        from django.db import connection

        if "crm_choiceoption" in connection.introspection.table_names():
            from .models import ChoiceOption

            for o in ChoiceOption.objects.all():
                edits.setdefault(o.list_key, {})[o.value] = o
    except Exception:  # no database yet (startup, migrations): the code defaults
        edits = {}
    _cache["at"], _cache["edits"] = now, edits
    return edits


def options(key, include_hidden=True):
    """[(value, label, hidden, added)] for a list, in its edited order."""
    reg = _registry[key]
    mine = _edits().get(key, {})
    rows = []
    for i, (value, label) in enumerate(reg["defaults"]):
        o = mine.get(value)
        rows.append((o.position if o and o.position is not None else i, value, (o.label if o and o.label else label),
                     bool(o and o.hidden), False))
    for value, o in mine.items():
        if o.added:
            rows.append((o.position if o.position is not None else 1000, value, o.label or value, o.hidden, True))
    rows.sort(key=lambda r: r[0])
    return [(v, l, h, a) for _, v, l, h, a in rows if include_hidden or not h]


class OptionList(list):
    """A list of (value, label) that remembers which dropdown list it came from, so templates can drop the hidden
    options with `|offer:current_value` (crm_tags.offer)."""

    key = None

    def __getitem__(self, i):
        out = super().__getitem__(i)
        if isinstance(i, slice):
            out = OptionList(out)
            out.key = self.key
        return out


def pairs(cls_or_key, include_hidden=True):
    key = cls_or_key if isinstance(cls_or_key, str) else _by_cls.get(cls_or_key)
    if key not in _registry:
        return [(m.value, m.label) for m in cls_or_key] if not isinstance(cls_or_key, str) else []
    out = OptionList((v, l) for v, l, _, _ in options(key, include_hidden))
    out.key = key
    return out


def visible(option_list, current=None):
    """Template helper: the options minus hidden ones (keeping `current`)."""
    key = getattr(option_list, "key", None)
    if key is None:
        return option_list
    hidden = {v for v, _, h, _ in options(key) if h}
    cur = "" if current is None else str(current)
    return [(v, l) for v, l in option_list if v not in hidden or str(v) == cur]


def offer(cls, current=None):
    """The options to show in a dropdown: visible ones, plus the current value if it has been hidden."""
    key = _by_cls.get(cls)
    if key is None:
        return list(cls.choices)
    return [(v, l) for v, l, h, _ in options(key) if not h or v == current]


def lists():
    """Settings page: every editable list, grouped by area."""
    out = {}
    for key, reg in _registry.items():
        if key in SYSTEM:
            continue
        area, title = TITLES.get(key, ("Other", key.replace(".", " · ")))
        mine = _edits().get(key, {})
        out.setdefault(area, []).append({
            "key": key, "title": title, "open": key in OPEN, "count": len(options(key, include_hidden=False)),
            "edited": bool(mine), "used_in": sorted({f"{f.model._meta.verbose_name}: {f.verbose_name}" for f in reg["fields"]}),
        })
    return [(a, sorted(out[a], key=lambda x: x["title"])) for a in AREAS if a in out]


def info(key):
    reg = _registry.get(key)
    if not reg or key in SYSTEM:
        return None
    area, title = TITLES.get(key, ("Other", key.replace(".", " · ")))
    return {"key": key, "title": title, "area": area, "open": key in OPEN, "max_length": reg["max_length"],
            "defaults": dict(reg["defaults"]),
            "used_in": sorted({f"{f.model._meta.verbose_name}: {f.verbose_name}" for f in reg["fields"]})}


# --- logic that knows about added options ----------------------------------------------------------------------

def base(key, value):
    """The built-in option `value` works like: itself if built in, else what the team chose when adding it."""
    reg = _registry.get(key)
    if not reg or value in dict(reg["defaults"]):
        return value
    o = _edits().get(key, {}).get(value)
    return (o.behaves_as or value) if o else value


def _like(key, members):
    """Added options whose behaves_as is one of `members`."""
    return [v for v, o in _edits().get(key, {}).items() if o.added and o.behaves_as in members]


class Group(list):
    """A set of options the code treats alike (open stages, late stages...): `x in group` and `field__in=group`
    also take the options added to work like one of them."""

    def __init__(self, key, values=None):
        if values is None:  # rebuilt from plain values (Django's query compiler does type(x)(values))
            key, values = None, key
        super().__init__(values)
        self.key = key

    def __iter__(self):
        own = list(super().__iter__())
        return iter(own + _like(self.key, set(own)))

    def __contains__(self, value):
        own = list(super().__iter__())
        return value in own or base(self.key, value) in own

    def __len__(self):
        return len(list(iter(self)))


class Order(list):
    """An ordered list of options (furthest stage first...): .index() places an added option with the one it
    works like."""

    def __init__(self, key, values=None):
        if values is None:  # rebuilt from plain values (Django's query compiler does type(x)(values))
            key, values = None, key
        super().__init__(values)
        self.key = key

    def index(self, value, *args):
        return super().index(base(self.key, value), *args)

    def __contains__(self, value):
        return super().__contains__(base(self.key, value))


class Lookup(dict):
    """{option: number} (stage order, win probability): an added option gets its look-alike's number."""

    def __init__(self, key, data):
        super().__init__(data)
        self.key = key

    def __missing__(self, value):
        b = base(self.key, value)
        if b != value and dict.__contains__(self, b):
            return self[b]
        raise KeyError(value)

    def get(self, value, default=None):
        try:
            return self[value]
        except KeyError:
            return default


# --- hooks ---------------------------------------------------------------------------------------------------------

def _choices(cls):
    key = _by_cls.get(cls)
    if key is None:
        return _original_choices.fget(cls)
    return pairs(key)


def _label(self):
    key = _by_cls.get(type(self))
    if key is not None:
        for v, l in pairs(key):
            if v == self.value:
                return l
    return self._label_


def _patch_field(field, key, defaults):
    field.choices = lambda: pairs(key)  # read fresh (from the cache) each time
    _field_key[(field.model, field.name)] = key
    original = field.deconstruct

    def deconstruct():
        name, path, args, kwargs = original()
        kwargs["choices"] = defaults  # migrations keep the code defaults
        return name, path, args, kwargs

    field.deconstruct = deconstruct


def install():
    from django.apps import apps
    from django.forms.models import BaseModelForm

    from . import models as crm_models

    if _registry:
        return
    classes = [o for o in vars(crm_models).values()
               if isinstance(o, type) and issubclass(o, models.TextChoices) and o is not models.TextChoices]
    models_ = list(apps.get_app_config("crm").get_models())
    for m in models_:
        classes += [o for o in vars(m).values() if isinstance(o, type) and issubclass(o, models.TextChoices)]
    for cls in classes:
        key = key_of(cls)
        defaults = [(m.value, m._label_) for m in cls]
        _registry[key] = {"cls": cls, "defaults": defaults, "fields": [], "max_length": 0}
        _by_cls[cls] = key
    for m in models_:
        for f in m._meta.get_fields():
            if f.is_relation or not getattr(f, "choices", None) or not isinstance(f, models.CharField):
                continue
            current = [tuple(c) for c in f.choices]
            for key, reg in _registry.items():
                if current == [tuple(c) for c in reg["defaults"]]:
                    reg["fields"].append(f)
                    reg["max_length"] = min(reg["max_length"] or f.max_length, f.max_length)
                    _patch_field(f, key, list(reg["defaults"]))
                    break
    enums.ChoicesType.choices = property(_choices)
    enums.Choices.label = enums.enum_property(_label)

    original_init = BaseModelForm.__init__

    def form_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        for name, ff in self.fields.items():
            key = _field_key.get((self._meta.model, name))
            if key is None or not hasattr(ff, "choices"):
                continue
            mf = self._meta.model._meta.get_field(name)
            current = getattr(self.instance, name, None) if self.instance is not None else None
            if self.is_bound:
                current = self.data.get(self.add_prefix(name), current)
            opts = [(v, l) for v, l, h, _ in options(key) if not h or v == current]
            ff.choices = ([("", "---------")] if (mf.blank or not ff.required) else []) + opts

    BaseModelForm.__init__ = form_init
