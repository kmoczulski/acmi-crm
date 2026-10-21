"""The directory: CapitalProvider becomes Firm (lenders and investors plus consultants, lawyers, engineers and
vendors), CapitalEmail becomes FirmEmail, and a project team member links to its firm."""
import django.db.models.deletion
from django.db import migrations, models


def rename_list_key(apps, schema_editor):
    apps.get_model("crm", "ChoiceOption").objects.filter(list_key="CapitalProvider.Kind").update(list_key="Firm.Kind")


def link_team(apps, schema_editor):
    """Team members already entered get their firm in the directory (found by name, else started)."""
    Firm, TeamMember = apps.get_model("crm", "Firm"), apps.get_model("crm", "TeamMember")
    kinds = {"architect": "architect", "civil": "engineer", "structural": "engineer", "mep": "engineer",
             "geotech": "engineer", "surveyor": "surveyor", "environmental": "environmental", "qd": "consultant",
             "gc": "contractor", "owners_rep": "consultant", "attorney": "law"}
    for t in TeamMember.objects.filter(directory__isnull=True).exclude(firm=""):
        name = " ".join(t.firm.split())
        t.directory = Firm.objects.filter(name__iexact=name).first() or Firm.objects.create(
            name=name, kind=kinds.get(t.role, "other"))
        t.save(update_fields=["directory"])


def unrename_list_key(apps, schema_editor):
    apps.get_model("crm", "ChoiceOption").objects.filter(list_key="Firm.Kind").update(list_key="CapitalProvider.Kind")


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0052_capital"),
    ]

    operations = [
        migrations.RenameModel("CapitalProvider", "Firm"),
        migrations.RenameModel("CapitalEmail", "FirmEmail"),
        migrations.RunPython(rename_list_key, unrename_list_key),
        migrations.AddField(
            model_name='teammember',
            name='directory',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='team_roles', to='crm.firm'),
        ),
        migrations.AlterField(
            model_name='firm',
            name='kind',
            field=models.CharField(choices=[('bank', 'Bank'), ('debt_fund', 'Debt fund / private lender'), ('broker', 'Debt broker / advisor'), ('investor', 'Equity investor'), ('agency', 'Agency / public program'), ('law', 'Law firm'), ('architect', 'Architect'), ('engineer', 'Engineer'), ('environmental', 'Environmental consultant'), ('surveyor', 'Surveyor'), ('contractor', 'Contractor / builder'), ('consultant', 'Consultant / advisor'), ('re_broker', 'Real estate broker'), ('title', 'Title / escrow'), ('accounting', 'Accountant / tax'), ('insurance', 'Insurance'), ('vendor', 'Vendor / supplier'), ('other', 'Other')], default='other', max_length=14),
        ),
        migrations.RunPython(link_team, migrations.RunPython.noop),
    ]
