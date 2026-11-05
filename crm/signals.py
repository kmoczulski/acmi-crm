from django.contrib.auth import get_user_model
from django.db.models.signals import m2m_changed, post_delete, post_save
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


@receiver(post_save, sender=Activity)
def follow_up_answered(sender, instance, created, raw=False, **kwargs):
    """An email just logged (sent from the hub, or read by the mail sync): tick the Follow-ups tasks it answers."""
    if created and not raw and instance.kind in (Activity.Kind.EMAIL_SENT, Activity.Kind.EMAIL_RECEIVED,
                                                 Activity.Kind.REPLY, Activity.Kind.AUTO_REPLY):
        from .pipeline import close_answered_tasks

        close_answered_tasks(instance)


@receiver(post_delete, sender="crm.Task")
def follow_up_task_deleted(sender, instance, **kwargs):
    """A Follow-ups task deleted by hand is not made again the next time the list loads."""
    if instance.source_activity_id and instance.assignee_id:
        from . import pipeline, prefs

        try:
            owner = instance.assignee
            gone = prefs.get_prefs(owner).get(pipeline.TASKS_DECLINED, [])
            if instance.source_activity_id not in gone:
                prefs.set_pref(owner, pipeline.TASKS_DECLINED, [*gone, instance.source_activity_id][-500:])
        except Exception:  # the person is being deleted too: nothing to remember
            pass


@receiver(post_save, sender="crm.Opportunity")
@receiver(post_save, sender="crm.LandSite")
@receiver(post_save, sender="crm.CapitalSource")
@receiver(post_save, sender="crm.Approval")
@receiver(post_save, sender="crm.Contract")
def record_board_follows(sender, instance, raw=False, **kwargs):
    # A status changed anywhere moves the record's card on record boards (crm/boards/bound.py).
    if raw:
        return
    from .boards.bound import record_saved

    record_saved(instance)


@receiver(post_save, sender="crm.ChoiceOption")
@receiver(post_delete, sender="crm.ChoiceOption")
def dropdown_edited(sender, **kwargs):
    from . import choices

    choices.clear()


@receiver(post_save, sender=get_user_model())
def mail_off_when_deactivated(sender, instance, **kwargs):
    """An account switched off loses Mail in the hub: the Gmail permission is revoked and the token deleted."""
    if not instance.is_active:
        _mail_off_if_on(instance)


@receiver(m2m_changed, sender=get_user_model().groups.through)
def mail_off_for_partners(sender, instance, action, reverse, pk_set, **kwargs):
    """Someone moved into the Partners group (capital partners) never keeps Mail in the hub. Works both ways round:
    user.groups.add(partners) and partners.user_set.add(user)."""
    if action != "post_add":
        return
    from .permissions import PARTNERS

    if reverse:  # instance is the group, pk_set the users
        users = get_user_model().objects.filter(pk__in=pk_set or ()) if instance.name == PARTNERS else []
    else:
        users = [instance] if instance.groups.filter(name=PARTNERS).exists() else []
    for user in users:
        user.__dict__.pop("_acmi_is_partner", None)
        _mail_off_if_on(user)


def _mail_off_if_on(user):
    from .models import MailConnection

    if MailConnection.objects.filter(user=user, enabled=True).exists():
        from .mail import connect

        connect.turn_off(user)
