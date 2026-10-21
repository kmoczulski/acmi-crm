from django import forms

from ..forms import StyledForm
from ..models import Firm
from . import logic


def parse_domains(raw):
    raw = (raw or "").replace(";", ",").replace(" ", ",")
    return [d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()]


class FirmForm(StyledForm):
    domains_text = forms.CharField(required=False, label="Email domains",
                                   help_text="Separated by commas, e.g. cozen.com. Emails with these domains are filed "
                                             "here, and meetings with them show here.")

    class Meta:
        model = Firm
        fields = ["name", "kind", "website", "notes"]
        labels = {"kind": "Type"}
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["domains_text"].initial = ", ".join(self.instance.domains or [])
        self.order_fields(["name", "kind", "domains_text", "website", "notes"])

    def clean_domains_text(self):
        domains = parse_domains(self.cleaned_data.get("domains_text"))
        bad = [d for d in domains if "." not in d or "@" in d]
        if bad:
            raise forms.ValidationError(f"Not a domain: {', '.join(bad)}")
        taken = Firm.objects.exclude(pk=self.instance.pk).filter(
            pk__in=[pk for d, pk in logic.firm_domains().items() if d in domains])
        if taken:
            raise forms.ValidationError(f"Already on {', '.join(f.name for f in taken)}.")
        company = logic.company_with_domain(domains)
        if company:
            raise forms.ValidationError(f"{company.name} is a Company with that domain. Use Move to directory on its "
                                        "page, so its people and emails come over and it leaves outreach.")
        return domains

    def save(self, commit=True):
        firm = super().save(commit=False)
        firm.domains = self.cleaned_data["domains_text"]
        if commit:
            firm.save()
        return firm
