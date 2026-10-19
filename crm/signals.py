from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .interactions import recompute_for_activity
from .models import Activity, suppress_history


@receiver(post_save, sender=Activity)
@receiver(post_delete, sender=Activity)
def activity_changed(sender, instance, **kwargs):
    # Bulk jobs recompute once at the end instead.
    if suppress_history.get():
        return
    if instance.kind in Activity.INTERACTION_KINDS or instance.kind in Activity.RESPONSE_KINDS:
        recompute_for_activity(instance)


@receiver(post_save, sender=Activity)
def reply_notification(sender, instance, created, **kwargs):
    if created and instance.kind == Activity.Kind.REPLY and not suppress_history.get():
        from .notify import reply_received

        reply_received(instance)


@receiver(post_save, sender="crm.ChoiceOption")
@receiver(post_delete, sender="crm.ChoiceOption")
def dropdown_edited(sender, **kwargs):
    from . import choices

    choices.clear()
