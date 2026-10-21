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
    if comment.site_id:
        people.add(comment.site.owner)
        people.update(comment.site.team.all())
    if comment.contract_id:
        people.add(comment.contract.owner)
        people.add(comment.contract.created_by)
    if comment.parent_id:
        people.add(comment.parent.author)
        people.update(r.author for r in comment.parent.replies.exclude(pk=comment.pk).select_related("author"))
    if (comment.company_id and not comment.opportunity_id and not comment.task_id and not comment.site_id
            and not comment.contract_id):
        people.add(comment.company.owner)
    if not comment.contract_id:
        people.update(c.author for c in Comment.objects.filter(
            company=comment.company_id, opportunity=comment.opportunity_id, task=comment.task_id, site=comment.site_id,
            contract=None,
        ).exclude(pk=comment.pk).select_related("author")[:20])
    return {p for p in people if p}


def comment_added(comment):
    record = comment.record()
    url = comment.link()
    if comment.document_id:
        record = f"{comment.document.title} ({record})"
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


def timed_sweep(user, now=None):
    """Tasks with a time: a reminder 15 minutes ahead and one when the time arrives. Runs on every poll."""
    from datetime import timedelta

    now = now or timezone.localtime()
    today = now.date()
    for t in Task.objects.filter(assignee=user, done=False, due_date=today, due_time__isnull=False):
        at = t.due_at
        when = f"{t.due_time:%I:%M %p}".lstrip("0")
        if at <= now:
            send(user, Notification.Kind.DUE, f"Due now ({when}): {t.title}", t.get_absolute_url(), None,
                 key=f"due-at:{t.pk}:{today}:{t.due_time:%H%M}")
        elif at - now <= timedelta(minutes=15):
            send(user, Notification.Kind.DUE, f"Due at {when}: {t.title}", t.get_absolute_url(), None,
                 key=f"due-soon:{t.pk}:{today}:{t.due_time:%H%M}")


def due_sweep(user):
    """Once a day per user: tasks due today or overdue, contract deadlines, diligence and closing items, and
    entitlement hearings, lapsing approvals and conditions, and capital: offers expiring and next steps. Keyed so
    nothing repeats."""
    today = timezone.localdate()
    for t in Task.objects.filter(assignee=user, done=False, due_date__lte=today):
        late = (today - t.due_date).days
        text = f"Due today: {t.title}" if late == 0 else f"Overdue {late} day{'s' if late != 1 else ''}: {t.title}"
        send(user, Notification.Kind.DUE, text, t.get_absolute_url(), None, key=f"due:{t.pk}:{today}")
    from .capital import logic as capital
    from .contracts import closing, dates
    from .entitlements import logic as entitlements
    from .land import diligence

    dates.reminders(user, today)  # contract deadlines on the deals they run
    diligence.reminders(user, today)  # due-diligence items they chase
    closing.reminders(user, today)  # closing checklist items
    entitlements.reminders(user, today)  # hearings, lapsing approvals, conditions due
    capital.reminders(user, today)  # term sheet offers expiring, lenders' next steps
