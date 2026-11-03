from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0076_chatconnection_can_create'),
    ]

    operations = [
        migrations.AddField(
            model_name='comment',
            name='photo',
            field=models.ImageField(blank=True, help_text='Optional photo attached to the comment', null=True, upload_to='comments/%Y/%m/'),
        ),
    ]
