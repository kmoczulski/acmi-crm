import re

from django import forms

from .models import (
    Campus, CampusPlan, Company, Document, FitScore, List, Note, Opportunity, OpportunityContact, Person, Project, Task,
)

# A saved number's label, as crm/mail/signature_phones.py writes it: "301-555-0142 (mobile)".
PHONE_LABEL = re.compile(r"\s*\((office|mobile)\)$")


def split_phones(phones):
    """The office and cell numbers (label removed) and the rest, which keep their text."""
    office = cell = ""
    rest = []
    for ph in phones or []:
        m = PHONE_LABEL.search(ph)
        if m and m.group(1) == "office" and not office:
            office = ph[:m.start()]
        elif m and m.group(1) == "mobile" and not cell:
            cell = ph[:m.start()]
        else:
            rest.append(ph)
    return office, cell, rest


class CommaListField(forms.CharField):
    """Edits a JSON list as comma-separated text."""

    def prepare_value(self, value):
        if isinstance(value, (list, tuple)):
            return ", ".join(str(v) for v in value)
        return value

    def to_python(self, value):
        value = super().to_python(value)
        return [v.strip() for v in (value or "").split(",") if v.strip()]


class UserChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.get_full_name() or obj.username


def active_users():
    from .permissions import team_users

    return team_users().order_by("first_name", "username")


NUMBER_FIELDS = (forms.DecimalField, forms.IntegerField, forms.FloatField)


def plain_number(value):
    """"25,000,000" or "$1,250,000.50" as someone typed it -> "25000000" / "1250000.50"."""
    if not isinstance(value, str):
        return value
    return value.replace(",", "").replace("$", "").strip()


def comma_numbers(form):
    """Number fields accept thousands separators ("25,000,000") and render as text fields marked data-commas, which
    nav.js shows with separators as people type (a type=number field can't show "10,000"). Call once the form's
    fields exist; StyledForm and CommaNumbersMixin do."""
    names = [name for name, f in form.fields.items() if isinstance(f, NUMBER_FIELDS)]
    for name in names:
        widget = form.fields[name].widget
        if isinstance(widget, forms.NumberInput):
            attrs = {k: v for k, v in widget.attrs.items() if k not in ("min", "max", "step")}
            attrs.setdefault("inputmode", "decimal")
            attrs.setdefault("autocomplete", "off")
            attrs["data-commas"] = "1"
            form.fields[name].widget = forms.TextInput(attrs=attrs)
    if form.is_bound and names:
        data = form.data.copy()
        for name in names:
            key = form.add_prefix(name)
            if key in data:
                data[key] = plain_number(data[key])
        form.data = data


class CommaNumbersMixin:
    """For a ModelForm that isn't a StyledForm: its number fields take and show thousands separators."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        comma_numbers(self)


class StyledForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.Textarea):
                field.widget.attrs.setdefault("rows", 3)
            if "owner" in name or name == "assignee":
                field.queryset = active_users()
        comma_numbers(self)


class CompanyForm(StyledForm):
    domains = CommaListField(required=False)
    flags = CommaListField(required=False, help_text="Comma-separated, e.g. Exclude, NASA Contractor")
    categories = CommaListField(required=False)
    owner = UserChoiceField(queryset=active_users(), required=False)
    # One chip row standing in for campus_plan + campus_interest.
    campus_choice = forms.MultipleChoiceField(
        label="Campus of interest", required=False, widget=forms.CheckboxSelectMultiple,
        help_text="Nothing selected = not known yet",
    )

    class Meta:
        model = Company
        fields = [
            "name", "status", "priority", "owner", "sector", "fit_score", "domains",
            "hq_location", "website", "linkedin", "categories", "flags", "description", "notes",
        ]
        widgets = {"fit_score": forms.RadioSelect}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        campuses = list(Campus.objects.order_by("code"))
        self.fields["campus_choice"].choices = [(c.code, c.code) for c in campuses] + [
            (CampusPlan.OWN, "Own campus"), (CampusPlan.NONE, "None"),
        ]
        self.order_fields(["name", "status", "priority", "owner", "sector", "fit_score", "campus_choice"])
        if self.instance.pk:
            plan = self.instance.campus_plan
            self.initial["campus_choice"] = (
                [c.code for c in self.instance.campus_interest.all()] if plan == CampusPlan.ACMI
                else [plan] if plan else []
            )
        fit = self.fields["fit_score"]
        fit.choices = [(v, str(v)) for v, _ in FitScore.choices]
        fit.required = False
        fit.help_text = ""
        fit.widget.attrs["data-definitions"] = "|".join(label.split(" · ", 1)[1] for _, label in FitScore.choices)

    def clean_domains(self):
        from .directory.logic import firm_domains

        domains = self.cleaned_data.get("domains") or []
        taken = sorted({d.lower() for d in domains} & set(firm_domains()))
        if taken:
            raise forms.ValidationError(f"{', '.join(taken)} belongs to a firm in the Directory (a consultant, vendor "
                                        "or lender), which never becomes a Company.")
        return domains

    def clean_campus_choice(self):
        picked = self.cleaned_data["campus_choice"]
        special = [p for p in picked if p in (CampusPlan.OWN, CampusPlan.NONE)]
        if special and len(picked) > 1:
            raise forms.ValidationError("Pick ACMI campuses, or Own campus, or None.")
        return picked

    def save(self, commit=True):
        picked = self.cleaned_data.get("campus_choice") or []
        company = self.instance
        if picked and picked[0] in (CampusPlan.OWN, CampusPlan.NONE):
            company.campus_plan = picked[0]
        else:
            company.campus_plan = CampusPlan.ACMI if picked else CampusPlan.UNKNOWN
        company = super().save(commit)
        codes = [p for p in picked if p not in (CampusPlan.OWN, CampusPlan.NONE)]

        def save_campuses():
            company.campus_interest.set(Campus.objects.filter(code__in=codes))

        if commit:
            save_campuses()
        else:
            self.save_m2m = save_campuses
        return company


class PersonForm(StyledForm):
    other_emails = CommaListField(required=False)
    phones = CommaListField(required=False, label="Other phones")
    office_phone = forms.CharField(required=False, label="Office phone")
    cell_phone = forms.CharField(required=False, label="Cell phone")
    flags = CommaListField(required=False)
    owner = UserChoiceField(queryset=active_users(), required=False)

    class Meta:
        model = Person
        fields = [
            "first_name", "last_name", "email", "title", "company", "priority", "owner", "phones",
            "other_emails", "linkedin", "email_valid", "flags", "notes",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["company"].queryset = Company.objects.only("id", "name").order_by("name")
        office, cell, rest = split_phones(self.instance.phones)
        self.initial.update(office_phone=office, cell_phone=cell, phones=rest)

    def clean(self):
        data = super().clean()
        phones = list(data.get("phones") or [])
        for key, label in (("office_phone", "office"), ("cell_phone", "mobile")):
            number = (data.get(key) or "").strip()
            if number:
                phones.append(f"{number} ({label})")
        data["phones"] = phones
        return data

    def save(self, commit=True):
        # full_name is derived from first/last when they change.
        self.instance.full_name = ""
        return super().save(commit)


class OpportunityForm(StyledForm):
    owner = UserChoiceField(queryset=active_users(), required=False)
    team = forms.ModelMultipleChoiceField(
        queryset=active_users(), required=False, widget=forms.CheckboxSelectMultiple, label="Deal team",
    )

    class Meta:
        model = Opportunity
        fields = [
            "name", "company", "stage", "probability", "priority", "owner", "team", "campus", "site", "deal_type",
            "asset_type", "value", "annual_rent", "lease_doc_kind", "lease_doc_url", "sq_ft_need", "acreage_need", "power_need_mw", "next_step", "next_step_due",
            "target_close", "est_delivery", "timeline", "lead_source", "investor_report", "partner_note", "trello_url",
            "notes",
        ]
        widgets = {
            "partner_note": forms.Textarea(attrs={"rows": 3}),
            "next_step_due": forms.DateInput(attrs={"type": "date"}),
            "target_close": forms.DateInput(attrs={"type": "date"}),
            "est_delivery": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["company"].queryset = Company.objects.only("id", "name").order_by("name")
        self.fields["team"].label_from_instance = lambda u: u.get_full_name() or u.username


class OpportunityContactForm(forms.Form):
    person = forms.ModelChoiceField(queryset=Person.objects.none())
    role = forms.ChoiceField(choices=OpportunityContact.Role.choices)
    note = forms.CharField(required=False, max_length=200)

    def __init__(self, *args, opportunity=None, **kwargs):
        super().__init__(*args, **kwargs)
        people = Person.objects.filter(company_id=opportunity.company_id) if opportunity else Person.objects.none()
        self.fields["person"].queryset = people.order_by("full_name", "email")
        self.fields["note"].widget.attrs["placeholder"] = "Note (optional)"


class DocumentForm(forms.ModelForm):
    class Meta:
        model = Document
        fields = ["title", "category", "url", "file"]
        labels = {"url": "Google Drive link"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["url"].widget.attrs["placeholder"] = "https://drive.google.com/…"
        self.fields["title"].required = False

    def clean(self):
        data = super().clean()
        if not data.get("url") and not data.get("file"):
            raise forms.ValidationError("Paste a Google Drive link or choose a file.")
        if not data.get("title"):
            upload = data.get("file")
            data["title"] = upload.name if upload else "Linked document"
            self.instance.title = data["title"]
        return data


class TaskForm(StyledForm):
    assignee = UserChoiceField(queryset=active_users(), required=False)

    class Meta:
        model = Task
        fields = ["title", "label", "due_date", "due_time", "assignee", "repeat", "repeat_until", "notes"]
        widgets = {
            "due_date": forms.DateInput(attrs={"type": "date"}),
            "due_time": forms.TimeInput(attrs={"type": "time", "title": "Time (optional)"}, format="%H:%M"),
            "repeat_until": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, full=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["title"].widget.attrs.setdefault("placeholder", "New task")
        if not full:  # the one-line "add task" form
            del self.fields["repeat_until"]
            del self.fields["notes"]
            del self.fields["label"]
            self.fields["repeat"].widget.attrs["title"] = "Repeat"


class NoteForm(forms.ModelForm):
    class Meta:
        model = Note
        fields = ["body"]
        widgets = {"body": forms.Textarea(attrs={"rows": 3, "placeholder": "Add a note…"})}


class CampusForm(forms.ModelForm):
    class Meta:
        model = Campus
        fields = ["code", "name", "location", "state", "acreage", "status"]


class ProjectForm(forms.ModelForm):
    keywords = CommaListField(required=False, help_text="Comma-separated, e.g. MEIH, Maryland Energetics Innovation Hub")

    class Meta:
        model = Project
        fields = ["name", "campus", "parent", "address", "keywords", "description", "active"]
        labels = {"parent": "Part of (optional)"}
        help_texts = {"campus": "The campus this project sits in"}
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["parent"].queryset = Project.objects.exclude(pk=self.instance.pk)

    def save(self, commit=True):
        project = super().save(commit=False)
        # A project in a campus is tagged with the campus too: hang it off the campus's own project.
        if project.campus and not project.parent:
            home = Project.objects.filter(campus=project.campus, parent__isnull=True).exclude(pk=project.pk).first()
            if home:
                project.parent = home
        if commit:
            project.save()
        return project


class ListForm(forms.ModelForm):
    stages = CommaListField(required=False, help_text="Kanban columns, comma-separated")

    class Meta:
        model = List
        fields = ["name", "object_type", "description", "stages"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # "Land search map" is only for saved map views, not lists.
        self.fields["object_type"].choices = [c for c in self.fields["object_type"].choices if c[0] != "land"]
