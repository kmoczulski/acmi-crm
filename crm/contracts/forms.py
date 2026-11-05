from django import forms

from ..forms import CommaNumbersMixin, StyledForm, UserChoiceField, active_users
from ..models import (
    Campus, Contract, ContractDate, ContractDocument, ContractTemplate, LandSite, LeaseTerms, Opportunity, TermsRound,
)
from . import terms

MAX_UPLOAD_MB = 60


def _date():
    return forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class DealChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.company.name} · {obj.name}"


class ContractForm(StyledForm):
    owner = UserChoiceField(queryset=active_users(), required=False, label="Runs it")
    opportunity = DealChoiceField(queryset=Opportunity.objects.none(), required=False, label="Tenant deal")

    class Meta:
        model = Contract
        fields = [
            "title", "kind", "status", "counterparty", "our_entity", "site", "campus", "opportunity", "price", "acres",
            "effective_date", "waiting_on", "waiting_since", "next_step", "counsel", "owner", "notes",
        ]
        widgets = {"effective_date": _date(), "waiting_since": _date(), "notes": forms.Textarea(attrs={"rows": 4})}
        labels = {"site": "Land site", "waiting_since": "Since"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["opportunity"].queryset = Opportunity.objects.select_related("company").order_by(
            "company__name", "name")
        self.fields["site"].queryset = LandSite.objects.select_related("campus").order_by("campus__code", "name")
        self.fields["campus"].queryset = Campus.objects.all()

    def clean(self):
        data = super().clean()
        if not data.get("campus"):
            data["campus"] = ((data.get("site") and data["site"].campus)
                              or (data.get("opportunity") and data["opportunity"].campus) or None)
        return data

    def save(self, commit=True):
        contract = super().save(commit=False)
        if contract.opportunity_id:
            contract.company = contract.opportunity.company
        if commit:
            contract.save()
            self.save_m2m()
        return contract


class DocumentForm(forms.ModelForm):
    MAX_MB = MAX_UPLOAD_MB
    MAX_BYTES = MAX_UPLOAD_MB * 1024 * 1024

    class Meta:
        model = ContractDocument
        fields = ["kind", "title", "file", "dated", "sent_by", "author", "url", "summary"]
        widgets = {"dated": _date(), "summary": forms.Textarea(attrs={"rows": 2})}
        labels = {"file": "File (PDF or Word)", "sent_by": "Draft sent by", "author": "Redlined by (optional)",
                  "url": "Link (optional)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["title"].required = False
        self.fields["title"].widget.attrs["placeholder"] = "Defaults to the file name, e.g. First Amendment to PSA"
        self.fields["summary"].widget.attrs["placeholder"] = "What it does, or what changed in this round (optional)"

    def clean_file(self):
        f = self.cleaned_data.get("file")
        if f and getattr(f, "size", 0) > MAX_UPLOAD_MB * 1024 * 1024:
            raise forms.ValidationError(f"Files up to {MAX_UPLOAD_MB} MB.")
        return f

    def clean(self):
        data = super().clean()
        upload = data.get("file")
        if not upload and not data.get("url") and not self.instance.file:
            raise forms.ValidationError("Choose a file (or paste a link to it).")
        if not data.get("title"):
            name = upload.name.rsplit("/", 1)[-1].rsplit(".", 1)[0] if upload else "Linked document"
            data["title"] = name.replace("_", " ")[:300]
            self.instance.title = data["title"]
        if data.get("kind") != ContractDocument.Kind.DRAFT:
            data["sent_by"] = ""
        return data


class DateForm(CommaNumbersMixin, forms.ModelForm):
    class Meta:
        model = ContractDate
        fields = ["kind", "label", "date", "amount", "rule", "critical", "key", "anchor", "offset", "unit", "extends",
                  "extend_by", "extend_unit"]
        widgets = {"date": _date()}
        labels = {"amount": "Amount ($)", "key": "Key (for counting from it)", "anchor": "Counts from",
                  "offset": "How many", "extends": "Extension: pushes out", "extend_by": "by", "extend_unit": "unit"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["label"].required = False
        self.fields["label"].widget.attrs["placeholder"] = "Defaults to the type, e.g. Second deposit due"
        self.fields["rule"].widget.attrs["placeholder"] = "e.g. 90 days after the Effective Date"
        self.fields["anchor"].widget.attrs["placeholder"] = "effective, or another date's key (read from the rule)"
        self.fields["extends"].widget.attrs["placeholder"] = "e.g. feasibility_end"

    def clean(self):
        data = super().clean()
        if not data.get("label") and data.get("kind"):
            data["label"] = ContractDate.Kind(data["kind"]).label
            self.instance.label = data["label"]
        return data


class LeaseTermsForm(CommaNumbersMixin, forms.ModelForm):
    class Meta:
        model = LeaseTerms
        fields = ["premises_sf", "land_acres", "commencement", "rent_start", "expiration", "base_rent",
                  "escalation_pct", "free_months", "renewals", "renewal_years", "renewal_notice_months",
                  "security_deposit", "ti_allowance", "guarantor"]
        widgets = {"commencement": _date(), "rent_start": _date(), "expiration": _date()}


class TemplateForm(forms.ModelForm):
    kinds = forms.MultipleChoiceField(choices=Contract.Kind.choices, required=False,
                                      widget=forms.CheckboxSelectMultiple, help_text="None ticked = any agreement")
    points_text = forms.CharField(label="Points to cover", widget=forms.Textarea(attrs={"rows": 14}),
                                  help_text='One per line, in order: "Label: what to cover"')

    class Meta:
        model = ContractTemplate
        fields = ["name", "description", "kinds", "instructions"]
        widgets = {"instructions": forms.Textarea(attrs={"rows": 3})}
        labels = {"instructions": "How to write it up"}


def _form_text(field, value):
    """A stored term as a form box shows it ("25,000", "182.99", "2026-11-13")."""
    if value is None or value == "":
        return ""
    if field.kind == "money":
        return f"{value:,.0f}" if value == value.to_integral_value() else f"{value:,.2f}"
    if field.kind == "acres":
        return format(value.normalize(), "f")
    if field.kind == "date":
        return value.isoformat()
    return str(value)


class TermsRoundForm(forms.Form):
    """One round of LOI / PSA terms. The terms boxes come from crm/contracts/terms.py (`r_<key>` for the deal,
    `p<n>_<key>` for phase n), so the form, the stored JSON, the schedule and the side-by-side grid can't drift apart.
    A spare, empty phase block is always offered; leave a phase empty and it is not saved."""

    GROUPS = ("Land and price", "Timeline", "Deposits", "Closing extensions")

    label = forms.CharField(max_length=120, label="Round", help_text="e.g. ACMI initial offer, Seller redline, ACMI counter")
    sent_by = forms.ChoiceField(choices=[("", "–"), *TermsRound.Side.choices], required=False, label="Proposal from")
    dated = forms.DateField(required=False, widget=_date(), label="Sent or received")
    document = forms.ModelChoiceField(queryset=ContractDocument.objects.none(), required=False,
                                      label="Draft or letter it summarizes")
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 3}), label="Notes")
    slots = forms.IntegerField(required=False, widget=forms.HiddenInput())

    def __init__(self, *args, contract, instance=None, source=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.contract, self.instance = contract, instance
        self.fields["document"].queryset = contract.documents.all()
        self.fields["document"].empty_label = "–"
        base = instance or source
        stored = terms.typed(base.terms) if base else terms.typed({})
        existing = [p for p in stored["phases"] if not terms.phase_is_empty(p)]
        posted = (self.data.get("slots") or "") if self.is_bound else ""
        self.slots = (min(max(int(posted), 1), terms.MAX_PHASES) if posted.isdigit()
                      else min(terms.MAX_PHASES, max(1, len(existing)) + 1))
        self.fields["slots"].initial = self.slots
        for f in terms.ROUND_FIELDS:
            self.fields[f"r_{f.key}"] = self._box(f)
            if not self.is_bound:
                self.initial.setdefault(f"r_{f.key}", _form_text(f, stored[f.key]))
        for i in range(self.slots):
            phase = existing[i] if i < len(existing) else {}
            for f in terms.phase_fields(i):
                self.fields[f"p{i + 1}_{f.key}"] = self._box(f, i)
                if not self.is_bound:
                    self.initial.setdefault(f"p{i + 1}_{f.key}", _form_text(f, phase.get(f.key)))
        if instance and not self.is_bound:
            for name in ("label", "sent_by", "dated", "document", "notes"):
                self.initial.setdefault(name, getattr(instance, name))
        self.used_phases = (
            sum(1 for i in range(self.slots) if any(self.data.get(f"p{i + 1}_{f.key}") for f in terms.phase_fields(i)
                                                    if f.key != "name"))
            if self.is_bound else len(existing))

    @staticmethod
    def _box(f, phase=None):
        attrs = {}
        if f.kind in ("money", "acres", "int"):
            attrs = {"inputmode": "numeric" if f.kind == "int" else "decimal", "autocomplete": "off"}
        if f.kind == "money":
            attrs["data-commas"] = "1"
        common = {"required": False, "label": f.label, "help_text": f.help}
        if f.kind == "date":
            return forms.DateField(widget=_date(), **common)
        if f.kind == "yesno":
            return forms.ChoiceField(choices=[("", "–"), ("yes", "Yes"), ("no", "No")], **common)
        if f.kind == "choice":
            return forms.ChoiceField(choices=[("", "Automatic"), *f.choices], **common)
        if f.kind == "text":
            attrs["placeholder"] = terms.phase_name(phase or 0)
            return forms.CharField(max_length=60, widget=forms.TextInput(attrs=attrs), **common)
        return forms.CharField(max_length=24, widget=forms.TextInput(attrs=attrs), **common)

    def _read(self, name, f):
        raw = self.cleaned_data.get(name)
        if f.kind in ("int", "money", "acres"):
            try:
                value = terms.coerce(f, raw, strict=True)
            except ValueError:
                self.add_error(name, "Enter a number, like 25,000")
                return None
            if value is not None and value < 0:
                self.add_error(name, "Can't be negative")
                return None
            return value
        return terms.coerce(f, raw)

    def clean(self):
        data = super().clean()
        t = {f.key: self._read(f"r_{f.key}", f) for f in terms.ROUND_FIELDS}
        phases = []
        for i in range(self.slots):
            n = i + 1
            ph = {f.key: None for f in terms.PHASE_FIELDS}
            ph.update({f.key: self._read(f"p{n}_{f.key}", f) for f in terms.phase_fields(i)})
            for part, whole in (("initial_nonref", "initial_deposit"), ("go_hard_nonref", "go_hard"),
                                ("close_ext_nonref", "close_ext_deposit")):
                if ph[part] and ph[part] > (ph[whole] or 0):
                    self.add_error(f"p{n}_{part}", "More than the deposit it is part of")
            for used, available in (("feas_ext_used", "feas_ext_options"), ("close_ext_used", "close_ext_options")):
                if ph[used]:
                    if ph[available] is None:
                        ph[available] = ph[used]  # "use 2" with nothing said about how many exist: there are 2
                    elif ph[used] > ph[available]:
                        self.add_error(f"p{n}_{used}", f"Only {ph[available]} available")
            phases.append(ph)
        t["phases"] = phases
        self.typed_terms = t
        return data

    def terms(self):
        """The JSON to store, once the form is valid."""
        return terms.dump(self.typed_terms)

    def deal_fields(self):
        return [self[f"r_{f.key}"] for f in terms.ROUND_FIELDS]

    def phase_blocks(self):
        blocks = []
        for i in range(self.slots):
            groups = []
            for g in self.GROUPS:
                boxes = [self[f"p{i + 1}_{f.key}"] for f in terms.phase_fields(i) if f.group == g and f.key != "name"]
                if boxes:
                    groups.append({"title": g, "fields": boxes})
            blocks.append({"n": i + 1, "name_field": self[f"p{i + 1}_name"], "groups": groups,
                           "open": i == 0 or i < self.used_phases})
        return blocks


class TermsImportForm(forms.Form):
    file = forms.FileField(label="LOI Tracker workbook (.xlsx)")

    def clean_file(self):
        f = self.cleaned_data["file"]
        if not f.name.lower().endswith((".xlsx", ".xlsm")):
            raise forms.ValidationError("Choose an Excel workbook (.xlsx).")
        if f.size > 10 * 1024 * 1024:
            raise forms.ValidationError("Workbooks up to 10 MB.")
        return f
