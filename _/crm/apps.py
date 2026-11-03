from django.apps import AppConfig


class CrmConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "crm"
    verbose_name = "ACMI CRM"

    def ready(self):
        from . import choices, signals, tokens  # noqa: F401

        choices.install()  # editable dropdown lists
