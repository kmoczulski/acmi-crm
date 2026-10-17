"""In-app notifications: the bell in the sidebar.

Sent when someone else assigns you a task, @mentions you, comments on something you own,
moves one of your deals, or a company you own replies; plus a once-a-day sweep for your
tasks that are due or overdue. Nobody is notified about their own actions.
"""
import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import Comment, Notification, Task

MENTION = re.compile(r"@([A-Za-z][\w.-]*)")


def send(user, kind, text, url="", actor=None, key=""):
    if user is None or (actor is not None and user.pk == actor.pk) or not user.is_active:
        return None
    if key and Notification.objects.filter(user=user, key=key).exists():
        return None
    return Notification.objects.create(user=user, kind=kind, text=text[:500], url=url[:500], actor=actor, key=key[:120])


def _name(user):
    return (user.get_full_name() or user.username) if user else "Someone"


def unread_count(user):
    return Notification.objects.filter(user=user, read_at__isnull=True).count()


def task_assigned(task, actor):
    if task.assignee_id and (actor is None or task.assignee_id != actor.pk):
        due = f" (due {task.due_date:%b} {task.due_date.day})" if task.due_date else ""
        send(task.assignee, Notification.Kind.ASSIGNED, f"{_name(actor)} gave you a task: {task.title}{due}",
             task.get_absolute_url(), actor)


def mentioned_users(text):
    """@kellen, @Simon or @simon.shewmaker -> users (username or first name, case-insensitive)."""
    users = list(get_user_model().objects.filter(is_active=True))
    found = set()
    for word in MENTION.findall(text or ""):
        w = word.lower().rstrip(".")
        for u in users:
            if w in (u.username.lower(), (u.first_name or "").lower(),
                     f"{u.first_name}.{u.last_name}".lower(), f"{u.first_name}{u.last_name}".lower()):
                found.add(u)
    return found


def watchers(comment):
    """People who care about a comment's record: task assignee, deal owner + team, company owner."""
    people = set()
    if comment.task_id:
        people.add(comment.task.assignee)
        people.add(comment.task.created_by)
    if comment.opportunity_id:
        people.add(comment.opportunity.owner)
        people.update(comment.opportunity.team.all())
    if comment.company_id and not comment.opportunity_id and not comment.task_id:
        people.add(comment.company.owner)
    people.update(c.author for c in Comment.objects.filter(
        company=comment.company_id, opportunity=comment.opportunity_id, task=comment.task_id,
    ).exclude(pk=comment.pk).select_related("author")[:20])
    return {p for p in people if p}


def comment_added(comment):
    record = comment.record()
    url = record.get_absolute_url() + "#comments"
    snippet = comment.body if len(comment.body) < 120 else comment.body[:117] + "…"
    mentioned = mentioned_users(comment.body)
    comment.mentions.set(mentioned)
    for u in mentioned:
        send(u, Notification.Kind.MENTION, f"{_name(comment.author)} mentioned you on {record}: “{snippet}”",
             url, comment.author, key=f"comment:{comment.pk}:{u.pk}")
    for u in watchers(comment) - mentioned:
        send(u, Notification.Kind.COMMENT, f"{_name(comment.author)} commented on {record}: “{snippet}”",
             url, comment.author, key=f"comment:{comment.pk}:{u.pk}")


def deal_stage_changed(opp, old_label, actor):
    people = {opp.owner, *opp.team.all()} - {None}
    for u in people:
        send(u, Notification.Kind.STAGE, f"{_name(actor)} moved {opp.name}: {old_label} → {opp.get_stage_display()}",
             opp.get_absolute_url(), actor)


def reply_received(activity):
    """A company someone owns (or is on the deal team for) wrote back. Only for fresh mail."""
    company = activity.company
    if company is None or activity.occurred_at < timezone.now() - timedelta(days=3):
        return
    people = {company.owner}
    for opp in company.opportunities.prefetch_related("team"):
        people.add(opp.owner)
        people.update(opp.team.all())
    who = str(activity.person) if activity.person_id else company.name
    for u in people - {None}:
        send(u, Notification.Kind.REPLY, f"{who} ({company.name}) replied: {activity.summary[:120]}",
             company.get_absolute_url() + "?tab=activity", None, key=f"reply:{activity.pk}:{u.pk}")


def due_sweep(user):
    """Once a day per user: tasks due today or overdue, and contract deadlines. Keyed so nothing repeats."""
    today = timezone.localdate()
    for t in Task.objects.filter(assignee=user, done=False, due_date__lte=today):
        late = (today - t.due_date).days
        text = f"Due today: {t.title}" if late == 0 else f"Overdue {late} day{'s' if late != 1 else ''}: {t.title}"
        send(user, Notification.Kind.DUE, text, t.get_absolute_url(), None, key=f"due:{t.pk}:{today}")
    from .contracts import dates

    dates.reminders(user, today)  # contract deadlines on the deals they run
