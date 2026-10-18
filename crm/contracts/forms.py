from django import forms

from ..forms import StyledForm, UserChoiceField, active_users
from ..models import (
    Campus, Contract, ContractDate, ContractDocument, ContractTemplate, LandSite, LeaseTerms, Opportunity,
)

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
    class Meta:
        model = ContractDocument
        fields = ["kind", "title", "file", "dated", "sent_by", "url", "summary"]
        widgets = {"dated": _date(), "summary": forms.Textarea(attrs={"rows": 2})}
        labels = {"file": "File (PDF or Word)", "sent_by": "Draft sent by", "url": "Link (optional)"}

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


class DateForm(forms.ModelForm):
    class Meta:
        model = ContractDate
        fields = ["kind", "label", "date", "amount", "rule"]
        widgets = {"date": _date()}
        labels = {"amount": "Amount ($)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["label"].required = False
        self.fields["label"].widget.attrs["placeholder"] = "Defaults to the type, e.g. Second deposit due"
        self.fields["rule"].widget.attrs["placeholder"] = "e.g. 90 days after the Effective Date"

    def clean(self):
        data = super().clean()
        if not data.get("label") and data.get("kind"):
            data["label"] = ContractDate.Kind(data["kind"]).label
            self.instance.label = data["label"]
        return data


class LeaseTermsForm(forms.ModelForm):
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
