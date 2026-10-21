from django import forms

from ..forms import StyledForm, UserChoiceField, active_users
from ..models import CapitalProvider, CapitalSource, Contract, Project, TermSheet
from . import logic

MAX_UPLOAD_MB = 60


def _date():
    return forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class SourceForm(StyledForm):
    """The provider is typed (the capital providers' names are offered): a known one links, a new name starts a
    provider. Providers are their own list, never Companies, so they stay out of outreach."""

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
        provider = CapitalProvider.objects.filter(name__iexact=name).first()
        if provider is None:
            kind = CapitalProvider.Kind.INVESTOR if source.kind in CapitalSource.EQUITY else (
                CapitalProvider.Kind.AGENCY if source.kind in ("grant", "tax_credit") else CapitalProvider.Kind.BANK)
            provider = CapitalProvider.objects.create(name=name, kind=kind, created_by=user)
        source.provider = provider
        if commit:
            source.save()
        return source


class ProviderForm(StyledForm):
    domains_text = forms.CharField(required=False, label="Email domains",
                                   help_text="Separated by commas, e.g. truist.com. Emails and meetings with these "
                                             "domains show on the provider.")

    class Meta:
        model = CapitalProvider
        fields = ["name", "kind", "website", "notes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["domains_text"].initial = ", ".join(self.instance.domains or [])

    def clean_domains_text(self):
        raw = (self.cleaned_data.get("domains_text") or "").replace(";", ",").replace(" ", ",")
        domains = [d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()]
        bad = [d for d in domains if "." not in d or "@" in d]
        if bad:
            raise forms.ValidationError(f"Not a domain: {', '.join(bad)}")
        taken = CapitalProvider.objects.exclude(pk=self.instance.pk).filter(
            pk__in=[pk for d, pk in logic.provider_domains().items() if d in domains])
        if taken:
            raise forms.ValidationError(f"Already on {', '.join(p.name for p in taken)}.")
        return domains

    def save(self, commit=True):
        provider = super().save(commit=False)
        provider.domains = self.cleaned_data["domains_text"]
        if commit:
            provider.save()
        return provider


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
