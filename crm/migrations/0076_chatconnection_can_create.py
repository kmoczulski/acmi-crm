from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0075_calendarsync_can_schedule'),
    ]

    operations = [
        migrations.AddField(
            model_name='chatconnection',
            name='can_create',
            field=models.BooleanField(default=False),
        ),
    ]
