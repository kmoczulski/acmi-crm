from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0074_chat_connection'),
    ]

    operations = [
        migrations.AddField(
            model_name='calendarsync',
            name='can_schedule',
            field=models.BooleanField(default=False, help_text="Allowed the hub to put meetings on their calendar (Google's calendar.events permission; docs/SCHEDULING.md)"),
        ),
    ]
