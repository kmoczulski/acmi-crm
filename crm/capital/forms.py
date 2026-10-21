from django import forms

from ..forms import StyledForm, UserChoiceField, active_users
from ..models import CapitalSource, Contract, Firm, Project, TermSheet
from . import logic

MAX_UPLOAD_MB = 60


def _date():
    return forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class SourceForm(StyledForm):
    """The provider is typed (the directory's lenders and investors are offered): a known firm links, a new name starts
    one in the directory. The directory is never Companies, so providers stay out of outreach."""

    provider_name = forms.CharField(max_length=200, label="Provider",
                                    widget=forms.TextInput(attrs={"list": "provider-names", "autocomplete": "off",
                                                                  "placeholder": "Lender, investor or agency"}))
    owner = UserChoiceField(queryset=active_users(), required=False, label="Runs it")

    class Meta:
        model = CapitalSource
        fields = ["project", "kind", "label", "status", "amount_sought", "amount_committed", "contact", "owner",
                  "waiting_on", "waiting_since", "next_step", "next_step_on", "contract", "notes"]
        widgets = {"waiting_since": _date(), "next_step_on": _date(), "notes": forms.Textarea(attrs={"rows": 3})}
        labels = {"waiting_since": "Since", "label": "Label (optional)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        inst = self.instance
        self.fields["project"].queryset = Project.objects.filter(active=True) | Project.objects.filter(
            pk=inst.project_id or 0)
        self.fields["contract"].queryset = Contract.objects.filter(
            kind__in=[Contract.Kind.LOAN, Contract.Kind.JV, Contract.Kind.OTHER]).order_by("title")
        if inst.provider_id:
            self.fields["provider_name"].initial = inst.provider.name
        self.order_fields(["provider_name"] + list(self.Meta.fields))

    def clean_provider_name(self):
        name = " ".join((self.cleaned_data.get("provider_name") or "").split())
        if not name:
            raise forms.ValidationError("Name the lender, investor or agency.")
        return name

    def save(self, commit=True, user=None):
        source = super().save(commit=False)
        name = self.cleaned_data["provider_name"]
        provider = Firm.objects.filter(name__iexact=name).first()
        if provider is None:
            kind = Firm.Kind.INVESTOR if source.kind in CapitalSource.EQUITY else (
                Firm.Kind.AGENCY if source.kind in ("grant", "tax_credit") else Firm.Kind.BANK)
            provider = Firm.objects.create(name=name, kind=kind, created_by=user)
        source.provider = provider
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
