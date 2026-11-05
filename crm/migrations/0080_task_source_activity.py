from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("crm", "0079_termsround")]

    operations = [
        migrations.AddField(
            model_name="task",
            name="source_activity",
            field=models.ForeignKey(blank=True, help_text="The email this task follows up on (made by the follow-ups list)",
                                    null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+",
                                    to="crm.activity"),
        ),
    ]
