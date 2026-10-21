from django import forms

from ..forms import StyledForm, UserChoiceField, active_users
from ..models import CapitalSource, Company, Contract, Person, Project, TermSheet, normalize_key
from . import logic

MAX_UPLOAD_MB = 60


def _date():
    return forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class SourceForm(StyledForm):
    """The provider is typed (with the CRM's company names offered): a known company links, anything else is kept as
    a name until someone adds it as a company."""

    provider = forms.CharField(max_length=200, required=False, label="Provider",
                               widget=forms.TextInput(attrs={"list": "company-names", "autocomplete": "off",
                                                             "placeholder": "Lender, investor or agency"}))
    owner = UserChoiceField(queryset=active_users(), required=False, label="Runs it")

    class Meta:
        model = CapitalSource
        fields = ["project", "kind", "label", "status", "amount_sought", "amount_committed", "owner", "waiting_on",
                  "waiting_since", "next_step", "next_step_on", "contract", "notes"]
        widgets = {"waiting_since": _date(), "next_step_on": _date(), "notes": forms.Textarea(attrs={"rows": 3})}
        labels = {"waiting_since": "Since", "label": "Label (optional)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        inst = self.instance
        self.fields["project"].queryset = Project.objects.filter(active=True) | Project.objects.filter(
            pk=inst.project_id or 0)
        self.fields["contract"].queryset = Contract.objects.filter(
            kind__in=[Contract.Kind.LOAN, Contract.Kind.JV, Contract.Kind.OTHER]).order_by("title")
        if inst.pk:
            self.fields["provider"].initial = inst.company.name if inst.company_id else inst.provider_name
        self.order_fields(["provider"] + list(self.Meta.fields))

    def clean(self):
        data = super().clean()
        name = " ".join((data.get("provider") or "").split())
        self.company = Company.objects.filter(name_key=normalize_key(name)).first() if name else None
        if not name:
            raise forms.ValidationError("Name the lender, investor or agency.")
        return data

    def save(self, commit=True):
        source = super().save(commit=False)
        source.company = self.company
        source.provider_name = "" if self.company else " ".join(self.cleaned_data["provider"].split())
        if commit:
            source.save()
        return source


class TermSheetForm(StyledForm):
    """Upload a version (file optional) or edit its terms."""

    class Meta:
        model = TermSheet
        fields = ["kind", "status", "title", "dated", "file"] + [f for f, _, _ in logic.TERMS] + ["notes"]
        widgets = {"dated": _date(), "expires_on": _date(), "close_by": _date(),
                   "covenants": forms.Textarea(attrs={"rows": 2}), "conditions": forms.Textarea(attrs={"rows": 2}),
                   "notes": forms.Textarea(attrs={"rows": 2})}
        labels = {"file": "File (PDF or Word)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["title"].widget.attrs["placeholder"] = "Defaults to the file name"

    def clean_file(self):
        f = self.cleaned_data.get("file")
        if f and getattr(f, "size", 0) > MAX_UPLOAD_MB * 1024 * 1024:
            raise forms.ValidationError(f"Files up to {MAX_UPLOAD_MB} MB.")
        return f


class ContactField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return obj.full_name or obj.email or f"Person {obj.pk}"


def contact_choices(source):
    """The provider's people, for the contact picker."""
    return Person.objects.filter(company_id=source.company_id).order_by("full_name") if source.company_id \
        else Person.objects.none()
