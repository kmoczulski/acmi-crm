from django import forms

from ..forms import StyledForm, UserChoiceField, active_users
from ..models import Approval, ApprovalDocument, Contract, LandSite, Project

MAX_UPLOAD_MB = 60


def _date():
    return forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class ApprovalForm(StyledForm):
    owner = UserChoiceField(queryset=active_users(), required=False, label="Runs it")

    class Meta:
        model = Approval
        fields = [
            "title", "kind", "status", "project", "site", "campus", "contract", "jurisdiction", "case_number",
            "consultant", "owner", "waiting_on", "waiting_since", "next_step", "needed_by", "submitted_on",
            "approved_on", "expires_on", "appeal_ends_on", "budget", "cost", "notes",
        ]
        widgets = {name: _date() for name in ("waiting_since", "needed_by", "submitted_on", "approved_on",
                                              "expires_on", "appeal_ends_on")}
        widgets["notes"] = forms.Textarea(attrs={"rows": 3})
        labels = {"waiting_since": "Since", "budget": "Budget ($)", "cost": "Spent ($)",
                  "contract": "Purchase agreement"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        inst = self.instance
        self.fields["project"].queryset = Project.objects.filter(active=True) | Project.objects.filter(
            pk=inst.project_id or 0)
        self.fields["site"].queryset = (LandSite.objects.exclude(status=LandSite.Status.DROPPED)
                                        | LandSite.objects.filter(pk=inst.site_id or 0)).order_by("name")
        self.fields["contract"].queryset = Contract.objects.filter(
            kind__in=[Contract.Kind.PSA, Contract.Kind.OPTION]).order_by("title")

    def clean(self):
        data = super().clean()
        if not data.get("campus"):
            data["campus"] = ((data.get("project") and data["project"].campus)
                              or (data.get("site") and data["site"].campus) or None)
        if not (data.get("project") or data.get("site") or data.get("campus")):
            raise forms.ValidationError("Pick the project, land site or campus it's for.")
        return data


class DocumentForm(forms.ModelForm):
    class Meta:
        model = ApprovalDocument
        fields = ["kind", "title", "file", "url", "dated"]
        widgets = {"dated": _date()}
        labels = {"file": "File (PDF or Word)", "url": "Link (optional)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["title"].required = False
        self.fields["title"].widget.attrs["placeholder"] = "Defaults to the file name"

    def clean_file(self):
        f = self.cleaned_data.get("file")
        if f and getattr(f, "size", 0) > MAX_UPLOAD_MB * 1024 * 1024:
            raise forms.ValidationError(f"Files up to {MAX_UPLOAD_MB} MB.")
        return f

    def clean(self):
        data = super().clean()
        upload = data.get("file")
        if not upload and not data.get("url"):
            raise forms.ValidationError("Choose a file (or paste a link to it).")
        if not data.get("title"):
            name = upload.name.rsplit("/", 1)[-1].rsplit(".", 1)[0] if upload else "Linked document"
            data["title"] = name.replace("_", " ")[:300]
            self.instance.title = data["title"]
        return data
