from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('crm', '0070_invite_versions'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ImportBatch',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('filename', models.CharField(max_length=255)),
                ('sheet', models.CharField(blank=True, max_length=120)),
                ('mode', models.CharField(choices=[('fill', 'Fill blanks only'), ('update', "Update with the sheet's values")], default='fill', max_length=10)),
                ('rows_read', models.PositiveIntegerField(default=0)),
                ('counts', models.JSONField(blank=True, default=dict)),
                ('results', models.JSONField(blank=True, default=list, help_text='What each row did, for the results page')),
                ('undo', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('undone_at', models.DateTimeField(blank=True, null=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('undone_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name_plural': 'import batches',
                'ordering': ['-created_at'],
            },
        ),
    ]
