"""The Managers group (crm/permissions.py). Everyone with an account when this ran keeps full access; accounts made
later (first Google sign-in) start without it until an admin adds them."""
from django.conf import settings
from django.db import migrations


def add_managers(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    group, _ = Group.objects.get_or_create(name="Managers")
    group.user_set.add(*User.objects.filter(is_active=True))


def remove_managers(apps, schema_editor):
    apps.get_model("auth", "Group").objects.filter(name="Managers").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("crm", "0031_senditem_sending_status"),
    ]

    operations = [migrations.RunPython(add_managers, remove_managers)]
