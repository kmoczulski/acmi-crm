"""Is this correspondence about a project (a campus or site) or is it prospecting?

Works per Gmail thread, because ACMI's own sent emails are stored by subject only:
  1. Prospecting: workbook campaign emails, news-outreach drafts, anything whose
     subject matches an outreach template, subjects ACMI sent to many companies
     (announcements, pitches), and replies in those threads.
  2. Project: the thread's own words (subject and the new part of each email, not
     quoted text or signatures) mention a project keyword. A site also tags its campus.
  3. Otherwise untagged.
"""
import re
from collections import defaultdict

from .mail.parse import new_text
from .models import Activity, EmailTemplate, NewsDraft, Project

EMAIL_KINDS = (Activity.Kind.EMAIL_SENT, Activity.Kind.EMAIL_RECEIVED, Activity.Kind.REPLY, Activity.Kind.AUTO_REPLY,
               Activity.Kind.BOUNCE)
SIGNATURE_START = re.compile(
    r"\n\s*(--|_{3,}|best|thanks|thank you|regards|sincerely|cheers|v/r|respectfully|sent from)\b", re.I
)
BLAST_COMPANIES = 8  # a subject sent to this many companies is a blast, not project work
INVITE = re.compile(r"^(updated |synced |new |canceled |cancelled )?(invitation|event|meeting)( updated)?\b|^(accepted|declined|tentative|canceled|cancelled)\b", re.I)
SUBJECT_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw)\s*:\s*)+", re.I)


def _norm_subject(subject):
    return " ".join(SUBJECT_PREFIX.sub("", subject or "").lower().split())


def own_words(activity):
    """Subject plus the new text of the email, without quoted thread or signature."""
    subject = activity.summary.split(": ", 1)[-1] if activity.kind in EMAIL_KINDS else activity.summary
    body = new_text(activity.body or "")
    body = SIGNATURE_START.split(body, maxsplit=1)[0]
    return f"{subject}\n{body}"


class Tagger:
    def __init__(self):
        self.projects = list(Project.objects.filter(active=True).select_related("parent"))
        self.patterns = []
        for p in self.projects:
            words = [w.strip() for w in (p.keywords or []) if w.strip()]
            if words:
                rx = re.compile(r"(?<![\w-])(" + "|".join(re.escape(w) for w in words) + r")(?![\w-])", re.I)
                self.patterns.append((p, rx))
        subjects = set(EmailTemplate.objects.values_list("subject", flat=True))
        subjects |= set(NewsDraft.objects.values_list("subject", flat=True))
        self.outreach_subjects = {_norm_subject(s) for s in subjects if s and len(s) > 8}
        self.blasts = self._blast_subjects()
        # Threads someone filed to a project by hand (crm/mail/filing.py): the tag stays whatever the keywords say.
        from .models import MailFiling

        self.filed = defaultdict(set)
        for thread_id, project_id in MailFiling.objects.filter(project__isnull=False).values_list("thread_id", "project"):
            self.filed[thread_id].add(project_id)

    @staticmethod
    def _blast_subjects():
        """Subjects ACMI sent to many different companies: announcements and pitches, i.e. prospecting.

        Meeting invitations are exempt; a project meeting can include many firms.
        """
        per_subject = defaultdict(set)
        sent = Activity.objects.filter(kind=Activity.Kind.EMAIL_SENT, source=Activity.Source.GMAIL,
                                       company__isnull=False)
        for summary, company_id in sent.values_list("summary", "company_id").iterator(chunk_size=5000):
            subject = _norm_subject(summary.split(": ", 1)[-1])
            if subject and not INVITE.match(subject):
                per_subject[subject].add(company_id)
        return {subject for subject, companies in per_subject.items() if len(companies) >= BLAST_COMPANIES}

    def projects_in(self, text):
        found = set()
        for project, rx in self.patterns:
            if rx.search(text):
                found.add(project)
                if project.parent_id:
                    found.add(project.parent)
        return found

    def is_outreach(self, activity):
        if activity.source == Activity.Source.IMPORT or activity.campaign:
            return True
        subject = _norm_subject(activity.summary.split(": ", 1)[-1])
        return subject in self.blasts or any(s in subject for s in self.outreach_subjects)

    @staticmethod
    def is_conversation(activities):
        """Someone outside ACMI wrote in this thread, or it is a meeting invitation."""
        inbound = (Activity.Kind.REPLY, Activity.Kind.EMAIL_RECEIVED)
        return any(
            a.kind in inbound or INVITE.match(_norm_subject(a.summary.split(": ", 1)[-1])) for a in activities
        ) or any(a.kind not in EMAIL_KINDS for a in activities)  # a note about a project counts

    def tag_group(self, activities):
        """Decide one context for a thread (or a single non-thread activity) and save it."""
        filed = self.filed.get(activities[0].thread_id) if activities[0].thread_id else None
        if filed:
            by_pk = {p.pk: p for p in Project.objects.filter(pk__in=filed)}
            projects = {by_pk[pk] for pk in filed if pk in by_pk}
            for a in activities:
                projects |= self.projects_in(own_words(a))
            for a in activities:
                if a.context != Activity.Context.PROJECT:
                    Activity.objects.filter(pk=a.pk).update(context=Activity.Context.PROJECT)
                a.projects.set(projects)
            return Activity.Context.PROJECT
        if any(self.is_outreach(a) for a in activities):
            context, projects = Activity.Context.PROSPECTING, set()
        else:
            projects = set()
            for a in activities:
                projects |= self.projects_in(own_words(a))
            context = Activity.Context.PROJECT if projects else Activity.Context.NONE
            if projects and not self.is_conversation(activities):
                # ACMI mentioned a campus but nobody wrote back: a pitch, not project work.
                context, projects = Activity.Context.PROSPECTING, set()
        for a in activities:
            if a.context != context:
                Activity.objects.filter(pk=a.pk).update(context=context)
            a.projects.set(projects)
        return context


def tag(activities=None, thread_ids=None):
    """Tag the given activities (or threads, or everything). Returns {context: count}."""
    tagger = Tagger()
    qs = Activity.objects.exclude(kind__in=[Activity.Kind.FIELD_CHANGE, Activity.Kind.STAGE_CHANGE,
                                             Activity.Kind.RECORD_CREATED, Activity.Kind.TASK])
    if thread_ids is not None:
        qs = qs.filter(thread_id__in=[t for t in thread_ids if t])
    elif activities is not None:
        qs = qs.filter(pk__in=[a.pk for a in activities])
    threads, singles = defaultdict(list), []
    for a in qs.only("id", "kind", "summary", "body", "source", "campaign", "thread_id", "context").iterator(
        chunk_size=2000
    ):
        (threads[a.thread_id].append(a) if a.thread_id else singles.append(a))
    counts = defaultdict(int)
    for group in threads.values():
        counts[tagger.tag_group(group)] += len(group)
    for a in singles:
        if a.kind == Activity.Kind.NEWS_TRIGGER:
            # News hooks feed outreach.
            if a.context != Activity.Context.PROSPECTING:
                Activity.objects.filter(pk=a.pk).update(context=Activity.Context.PROSPECTING)
            counts[Activity.Context.PROSPECTING] += 1
            continue
        counts[tagger.tag_group([a])] += 1
    return dict(counts)


def company_projects(company):
    """[(project, email count)] for a company, busiest first."""
    rows = defaultdict(int)
    for project_id in Activity.objects.filter(company=company, context=Activity.Context.PROJECT).values_list(
        "projects", flat=True
    ):
        if project_id:
            rows[project_id] += 1
    projects = Project.objects.in_bulk(list(rows))
    return sorted(((projects[pk], n) for pk, n in rows.items()), key=lambda r: (r[0].parent_id is not None, -r[1]))
