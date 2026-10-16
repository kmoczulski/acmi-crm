"""ACMI CRM data model. See docs/attio_spec.md, sections 1, 4 and 5."""
from contextvars import ContextVar
from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone

from .middleware import get_current_user

User = settings.AUTH_USER_MODEL

# Set to True by bulk jobs (the workbook import) so their writes don't flood history.
suppress_history = ContextVar("suppress_history", default=False)


def normalize_key(value):
    """Lower-case, whitespace-collapsed key used to dedupe company names."""
    return " ".join((value or "").lower().split())


class Priority(models.TextChoices):
    HIGH = "High", "High"
    MID = "Mid", "Mid"
    LOW = "Low", "Low"
    TEST = "Test", "Test"


class CompanyStatus(models.TextChoices):
    PROSPECT = "prospect", "Prospect"
    CONTACTED = "contacted", "Contacted"
    ENGAGED = "engaged", "Engaged"
    MEETING = "meeting", "Meeting held"
    OPPORTUNITY = "opportunity", "Opportunity"
    TENANT = "tenant", "Tenant"
    NOT_INTERESTED = "not_interested", "Not interested"
    DO_NOT_CONTACT = "do_not_contact", "Do not contact"


class FitScore(models.IntegerChoices):
    """How well a company matches what ACMI builds. Shown wherever it is edited."""

    POOR = 1, "1 · Poor: no realistic facility need (services only, wrong geography or sector)"
    WEAK = 2, "2 · Weak: little sign of a facility need, or a marginal sector fit"
    POSSIBLE = 3, "3 · Possible: adjacent sector or unclear need; worth nurturing"
    STRONG = 4, "4 · Strong: right sector and growing; expansion likely, size or timing unconfirmed"
    IDEAL = 5, "5 · Ideal: defense, aerospace or energetics maker with a funded need in the next 24 months"


class CampusPlan(models.TextChoices):
    UNKNOWN = "", "Not known yet"
    ACMI = "acmi", "An ACMI campus"
    OWN = "own", "Their own campus"
    NONE = "none", "None"


class OpportunityStage(models.TextChoices):
    """Same stages as the Fortress pipeline tracker (ACMI_Properties_Pipeline_Tracker.xlsx)."""

    PROSPECT = "prospect", "Prospect"
    QUALIFIED = "qualified", "Qualified"
    LOI_NEGOTIATION = "loi_neg", "LOI / PDA negotiation"
    LOI_SIGNED = "loi_signed", "LOI / PDA signed"
    LEASE_NEGOTIATION = "lease_neg", "Lease negotiation"
    SIGNED = "signed", "Signed lease / DMA"
    ON_HOLD = "on_hold", "On hold"
    LOST = "lost", "Cold / lost"


# Default win probability per stage, from the tracker's Lists sheet. Lease negotiation is
# not in the tracker; 85% sits between a signed LOI (75%) and a signed lease.
STAGE_PROBABILITY = {
    OpportunityStage.PROSPECT: 10,
    OpportunityStage.QUALIFIED: 25,
    OpportunityStage.LOI_NEGOTIATION: 50,
    OpportunityStage.LOI_SIGNED: 75,
    OpportunityStage.LEASE_NEGOTIATION: 85,
    OpportunityStage.SIGNED: 100,
    OpportunityStage.ON_HOLD: 5,
    OpportunityStage.LOST: 0,
}
OPEN_STAGES = [
    OpportunityStage.PROSPECT, OpportunityStage.QUALIFIED, OpportunityStage.LOI_NEGOTIATION,
    OpportunityStage.LOI_SIGNED, OpportunityStage.LEASE_NEGOTIATION,
]
STAGE_ORDER = {value: i for i, value in enumerate(OpportunityStage.values)}


class DealType(models.TextChoices):
    BUILD_TO_SUIT = "Build-to-Suit", "Build-to-suit"
    MULTI_TENANT = "Multi-Tenant", "Multi-tenant"
    MULTI_TENANT_BTS = "Multi-Tenant BTS", "Multi-tenant BTS"
    FEE_DEVELOPMENT = "Fee Development", "Fee development"
    OTHER = "Other", "Other"


class AssetType(models.TextChoices):
    INDUSTRIAL = "Industrial", "Industrial"
    FLEX = "Flex / Light Industrial", "Flex / light industrial"
    AEROSPACE = "Aerospace", "Aerospace"
    ENERGETICS = "Energetics / Munitions Mfg.", "Energetics / munitions mfg."
    LAND = "Land", "Land"
    SPECIAL = "Special Purpose (Defense)", "Special purpose (defense)"
    OTHER = "Other", "Other"


DOCUMENT_CATEGORIES = [
    "PDA", "LOI", "NDA", "Contract", "Lease / term sheet", "Proposal", "Site / plans", "Marketing", "Financials", "Other",
]


# ---------------------------------------------------------------------------
# Change tracking (Attio's is_historic)
# ---------------------------------------------------------------------------


class FieldHistory(models.Model):
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveBigIntegerField()
    field = models.CharField(max_length=64)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    changed_at = models.DateTimeField(default=timezone.now, db_index=True)
    changed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        indexes = [models.Index(fields=["content_type", "object_id", "field"])]
        ordering = ["-changed_at"]

    def __str__(self):
        return f"{self.field}: {self.old_value!r} -> {self.new_value!r}"


class TrackedModel(models.Model):
    """Writes FieldHistory (and a timeline Activity) when a tracked field changes.

    TRACKED_FIELDS holds attribute names (use `owner_id`, not `owner`).
    STAGE_FIELD changes show up on the timeline as foreground events; other
    tracked fields are background events.
    """

    TRACKED_FIELDS = ()
    STAGE_FIELD = None

    class Meta:
        abstract = True

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        loaded = set(field_names)
        instance._tracked_original = {
            f: getattr(instance, f) for f in cls.TRACKED_FIELDS if f.removesuffix("_id") in loaded or f in loaded
        }
        return instance

    def timeline_links(self):
        """Activity FKs for this record: {'company': ..., 'person': ..., 'opportunity': ...}."""
        return {}

    def _display_value(self, field, value):
        if value in (None, ""):
            return ""
        if field.endswith("_id"):
            rel = self._meta.get_field(field.removesuffix("_id")).related_model
            obj = rel.objects.filter(pk=value).first()
            if obj is None:
                return str(value)
            return (obj.get_full_name() if hasattr(obj, "get_full_name") else "") or str(obj)
        model_field = self._meta.get_field(field)
        if model_field.choices:
            return dict(model_field.flatchoices).get(value, str(value))
        return str(value)

    def save(self, *args, **kwargs):
        original = getattr(self, "_tracked_original", None)
        super().save(*args, **kwargs)
        current = {f: getattr(self, f) for f in self.TRACKED_FIELDS}
        if original is not None and not suppress_history.get():
            user = get_current_user()
            ct = ContentType.objects.get_for_model(self)
            for field, new in current.items():
                if field not in original or original[field] == new:
                    continue
                old_s = self._display_value(field, original[field])
                new_s = self._display_value(field, new)
                FieldHistory.objects.create(
                    content_type=ct, object_id=self.pk, field=field.removesuffix("_id"),
                    old_value=old_s, new_value=new_s, changed_by=user,
                )
                is_stage = field == self.STAGE_FIELD
                label = self._meta.get_field(field.removesuffix("_id")).verbose_name
                Activity.objects.create(
                    kind=Activity.Kind.STAGE_CHANGE if is_stage else Activity.Kind.FIELD_CHANGE,
                    occurred_at=timezone.now(),
                    actor=user,
                    is_background=not is_stage,
                    summary=f"{label.capitalize()} changed from {old_s or '—'} to {new_s or '—'}",
                    source=Activity.Source.USER,
                    **self.timeline_links(),
                )
        self._tracked_original = current


class InteractionFields(models.Model):
    """Computed from Activity by crm.interactions.recompute(). Never edit by hand."""

    first_interaction_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_interaction_at = models.DateTimeField(null=True, blank=True, editable=False, db_index=True)
    next_interaction_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_outreach_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_reply_at = models.DateTimeField(null=True, blank=True, editable=False)
    touch_count = models.PositiveIntegerField(default=0, editable=False)
    last_response = models.CharField(max_length=40, blank=True, editable=False)
    last_response_at = models.DateTimeField(null=True, blank=True, editable=False)
    # Attio's strongest_connection_user / _strength: the teammate who knows them best.
    strongest_connection_user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+", editable=False
    )
    connection_strength = models.CharField(max_length=10, blank=True, editable=False)

    class Meta:
        abstract = True

    @property
    def days_since_touch(self):
        if not self.last_interaction_at:
            return None
        return (timezone.now() - self.last_interaction_at).days


# ---------------------------------------------------------------------------
# Core objects
# ---------------------------------------------------------------------------


class Campus(models.Model):
    code = models.CharField(max_length=16, unique=True, help_text="Short code, e.g. NSIH")
    name = models.CharField(max_length=200)
    location = models.CharField(max_length=200, blank=True)
    state = models.CharField(max_length=2, blank=True)
    acreage = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=60, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "campuses"
        ordering = ["code"]

    def __str__(self):
        return self.code


class Project(models.Model):
    """A campus, site or other deal that correspondence can be about (see crm/projects.py)."""

    name = models.CharField(max_length=120, unique=True, help_text="Short label, e.g. MEIH or 4390 Indian Head Hwy")
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="sites",
        help_text="For a site: the campus it belongs to. Emails about the site are also tagged with the campus.",
    )
    campus = models.ForeignKey(Campus, null=True, blank=True, on_delete=models.SET_NULL, related_name="projects")
    address = models.CharField(max_length=255, blank=True)
    keywords = models.JSONField(
        default=list, blank=True,
        help_text="Words or phrases that mark an email as about this project (case-insensitive, whole words)",
    )
    description = models.TextField(blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = [models.F("parent").asc(nulls_first=True), "name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("crm:project_detail", args=[self.pk])


class Company(TrackedModel, InteractionFields):
    TRACKED_FIELDS = ("status", "priority", "owner_id", "fit_score", "campus_plan")
    STAGE_FIELD = "status"

    name = models.CharField(max_length=255)
    name_key = models.CharField(max_length=255, unique=True, editable=False)
    domains = models.JSONField(default=list, blank=True)
    description = models.TextField(blank=True)
    sector = models.CharField(max_length=120, blank=True, db_index=True)
    categories = models.JSONField(default=list, blank=True)
    priority = models.CharField(max_length=8, choices=Priority.choices, blank=True, db_index=True)
    fit_score = models.PositiveSmallIntegerField(
        null=True, blank=True, choices=FitScore.choices, validators=[MinValueValidator(1), MaxValueValidator(5)],
    )
    status = models.CharField(
        "stage", max_length=20, choices=CompanyStatus.choices, default=CompanyStatus.PROSPECT, db_index=True
    )
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="owned_companies")
    hq_location = models.CharField("HQ location", max_length=200, blank=True)
    website = models.URLField(blank=True)
    linkedin = models.URLField("LinkedIn", blank=True)
    flags = models.JSONField(default=list, blank=True, help_text="e.g. Exclude, NASA Contractor")
    campus_plan = models.CharField("campus of interest", max_length=10, choices=CampusPlan.choices, blank=True)
    campus_interest = models.ManyToManyField(
        Campus, blank=True, related_name="interested_companies", help_text="Which ACMI campuses, if any"
    )
    notes = models.TextField(blank=True)
    custom_fields = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        verbose_name_plural = "companies"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.name_key = normalize_key(self.name)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("crm:company_detail", args=[self.pk])

    def timeline_links(self):
        return {"company": self}

    @property
    def is_excluded(self):
        return "Exclude" in (self.flags or [])

    @property
    def campus_label(self):
        """NSIH, MEIH / Their own campus / None / "" (not known)."""
        if self.campus_plan == CampusPlan.ACMI:
            codes = ", ".join(c.code for c in self.campus_interest.all())
            return codes or "An ACMI campus"
        return "" if not self.campus_plan else self.get_campus_plan_display()


class Person(TrackedModel, InteractionFields):
    TRACKED_FIELDS = ("priority", "owner_id", "company_id", "title")

    first_name = models.CharField(max_length=120, blank=True)
    last_name = models.CharField(max_length=120, blank=True)
    full_name = models.CharField(max_length=255, blank=True)
    email = models.EmailField(max_length=255, null=True, blank=True, unique=True, help_text="Primary email (lower-case)")
    other_emails = models.JSONField(default=list, blank=True)
    phones = models.JSONField(default=list, blank=True)
    title = models.CharField(max_length=255, blank=True)
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.SET_NULL, related_name="people")
    linkedin = models.URLField("LinkedIn", blank=True)
    email_valid = models.BooleanField(null=True, blank=True)
    priority = models.CharField(max_length=8, choices=Priority.choices, blank=True, db_index=True)
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="owned_people")
    flags = models.JSONField(default=list, blank=True)
    notes = models.TextField(blank=True)
    custom_fields = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        verbose_name_plural = "people"
        ordering = ["full_name"]

    def __str__(self):
        return self.full_name or self.email or f"Person {self.pk}"

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.strip().lower()
        if not self.full_name:
            self.full_name = " ".join(p for p in (self.first_name, self.last_name) if p)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("crm:person_detail", args=[self.pk])

    def timeline_links(self):
        return {"person": self, "company_id": self.company_id}

    @property
    def is_excluded(self):
        return "Exclude" in (self.flags or [])


class Opportunity(TrackedModel):
    """Attio's Deal, shaped for ACMI: one tenant's space need at a campus or site."""

    TRACKED_FIELDS = ("stage", "value", "owner_id", "campus_id", "next_step")
    STAGE_FIELD = "stage"

    name = models.CharField(max_length=255)
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="opportunities")
    campus = models.ForeignKey(Campus, null=True, blank=True, on_delete=models.SET_NULL, related_name="opportunities")
    site = models.CharField(max_length=120, blank=True, help_text="When it is not an ACMI campus: TBD, Project North Star…")
    stage = models.CharField(max_length=20, choices=OpportunityStage.choices, default=OpportunityStage.PROSPECT, db_index=True)
    probability = models.PositiveSmallIntegerField(
        "win %", null=True, blank=True, validators=[MaxValueValidator(100)], help_text="Empty = the stage default",
    )
    priority = models.CharField(max_length=8, choices=Priority.choices, blank=True)
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="opportunities")
    team = models.ManyToManyField(User, blank=True, related_name="deal_teams", help_text="Everyone working the deal")
    deal_type = models.CharField(max_length=30, choices=DealType.choices, blank=True)
    asset_type = models.CharField(max_length=40, choices=AssetType.choices, blank=True)
    sq_ft_need = models.PositiveIntegerField("size (sq ft)", null=True, blank=True)
    acreage_need = models.DecimalField("acres", max_digits=10, decimal_places=2, null=True, blank=True)
    power_need_mw = models.DecimalField("power need (MW)", max_digits=8, decimal_places=2, null=True, blank=True)
    value = models.DecimalField("est. project cost ($)", max_digits=14, decimal_places=2, null=True, blank=True)
    lead_source = models.CharField("lead / source", max_length=120, blank=True)
    target_close = models.DateField(null=True, blank=True)
    est_delivery = models.DateField("est. delivery", null=True, blank=True)
    timeline = models.CharField(max_length=120, blank=True, help_text="e.g. Occupancy Q3 2027")
    next_step = models.CharField(max_length=300, blank=True)
    next_step_due = models.DateField(null=True, blank=True)
    investor_report = models.BooleanField(
        "on investor report", default=False, help_text="Show on the pipeline report sent to capital partners",
    )
    people = models.ManyToManyField(Person, blank=True, related_name="opportunities", through="OpportunityContact")
    notes = models.TextField(blank=True)
    trello_url = models.URLField(max_length=500, blank=True)
    source_key = models.CharField(max_length=120, null=True, blank=True, unique=True, editable=False,
                                  help_text="Import key, so re-running an import updates instead of duplicating")
    stage_changed_at = models.DateTimeField(default=timezone.now, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "opportunities"
        ordering = ["-updated_at"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        original = getattr(self, "_tracked_original", {})
        if original.get("stage") not in (None, self.stage):
            self.stage_changed_at = timezone.now()
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("crm:opportunity_detail", args=[self.pk])

    def timeline_links(self):
        return {"opportunity": self, "company_id": self.company_id}

    @property
    def is_open(self):
        return self.stage in OPEN_STAGES

    @property
    def win_probability(self):
        return self.probability if self.probability is not None else STAGE_PROBABILITY.get(self.stage, 0)

    @property
    def weighted_value(self):
        return (self.value or Decimal(0)) * self.win_probability / 100

    @property
    def site_label(self):
        return self.campus.code if self.campus_id else (self.site or "TBD")

    @property
    def cost_psf(self):
        return self.value / self.sq_ft_need if self.value and self.sq_ft_need else None


class OpportunityContact(models.Model):
    """Who is who on the tenant's side of a deal."""

    class Role(models.TextChoices):
        DECISION_MAKER = "decision_maker", "Decision maker"
        CHAMPION = "champion", "Champion"
        SITE_SELECTION = "site_selection", "Site selection / real estate"
        FINANCE = "finance", "Finance"
        LEGAL = "legal", "Legal"
        TECHNICAL = "technical", "Technical / facilities"
        BROKER = "broker", "Broker / advisor"
        INFLUENCER = "influencer", "Influencer"
        OTHER = "other", "Other"

    opportunity = models.ForeignKey(Opportunity, on_delete=models.CASCADE, related_name="contacts")
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="deal_roles")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.OTHER)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["opportunity", "person"], name="opportunity_contact_unique")]
        ordering = ["role", "person__full_name"]

    def __str__(self):
        return f"{self.person} ({self.get_role_display()})"


class Document(models.Model):
    """A file the team added by hand: a Google Drive link, or an uploaded file.

    Links are the default so nothing is stored twice; uploads go to local media in
    development and to Google Drive once the app is hosted (see crm/documents.py).
    Files exchanged by email are Attachment rows instead.
    """

    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.CASCADE, related_name="documents")
    opportunity = models.ForeignKey(
        Opportunity, null=True, blank=True, on_delete=models.CASCADE, related_name="documents",
    )
    title = models.CharField(max_length=300)
    category = models.CharField(max_length=40, blank=True, choices=[(c, c) for c in DOCUMENT_CATEGORIES])
    url = models.URLField("link", max_length=1000, blank=True, help_text="Google Drive (or any) link")
    file = models.FileField(upload_to="documents/%Y/%m/", blank=True)
    added_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-added_at"]

    def __str__(self):
        return self.title

    @property
    def href(self):
        # Uploads go through the app (crm.views.document_file): it checks sign-in and works with DEBUG off, where
        # /media/ isn't served.
        return self.url or (reverse("crm:document_file", args=[self.pk]) if self.file else "")


class DealSuggestionDismissal(models.Model):
    """'Not a deal' on a company suggested from its LOI/PDA/NDA emails."""

    company = models.OneToOneField(Company, on_delete=models.CASCADE, related_name="+")
    dismissed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    dismissed_at = models.DateTimeField(auto_now_add=True)


# ---------------------------------------------------------------------------
# Timeline: one table for every event (spec section 4)
# ---------------------------------------------------------------------------


class Activity(models.Model):
    class Kind(models.TextChoices):
        EMAIL_SENT = "email_sent", "Email sent"
        EMAIL_RECEIVED = "email_received", "Email received"
        REPLY = "reply", "Reply"
        BOUNCE = "bounce", "Bounce"
        AUTO_REPLY = "auto_reply", "Auto-reply"
        MEETING = "meeting", "Meeting"
        GRANOLA_NOTE = "granola_note", "Granola note"
        CALL = "call", "Call"
        NOTE = "note", "Note"
        TASK = "task", "Task"
        STAGE_CHANGE = "stage_change", "Stage change"
        FIELD_CHANGE = "field_change", "Field change"
        NEWS_TRIGGER = "news_trigger", "News trigger"
        RECORD_CREATED = "record_created", "Record created"

    class Source(models.TextChoices):
        GMAIL = "gmail", "Gmail"
        GRANOLA = "granola", "Granola"
        CALENDAR = "calendar", "Google Calendar"
        IMPORT = "import", "Workbook import"
        NEWS_SCAN = "news_scan", "News scan"
        USER = "user", "User"
        SYSTEM = "system", "System"

    # Kinds that count as a real interaction for the computed fields.
    INTERACTION_KINDS = (Kind.EMAIL_SENT, Kind.EMAIL_RECEIVED, Kind.REPLY, Kind.MEETING, Kind.GRANOLA_NOTE, Kind.CALL)
    # Kinds that set last_response (the workbook's "Response" column).
    RESPONSE_KINDS = (Kind.REPLY, Kind.BOUNCE, Kind.AUTO_REPLY)

    occurred_at = models.DateTimeField(db_index=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, db_index=True)
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, help_text="Empty = system")
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.CASCADE, related_name="activities")
    # Deleting a person or opportunity keeps its emails on the company timeline.
    person = models.ForeignKey(Person, null=True, blank=True, on_delete=models.SET_NULL, related_name="activities")
    opportunity = models.ForeignKey(
        Opportunity, null=True, blank=True, on_delete=models.SET_NULL, related_name="activities"
    )
    class Context(models.TextChoices):
        NONE = "", "Untagged"
        PROJECT = "project", "Project"
        PROSPECTING = "prospecting", "Prospecting"

    # What the correspondence is about; set by crm/projects.py per email thread.
    context = models.CharField(max_length=12, choices=Context.choices, blank=True, db_index=True)
    projects = models.ManyToManyField(Project, blank=True, related_name="activities")
    # Teammates whose mailbox holds this email (the same email can sit in several).
    team_members = models.ManyToManyField(User, blank=True, related_name="email_activities")
    is_background = models.BooleanField(default=False)
    summary = models.CharField(max_length=500)
    body = models.TextField(blank=True)
    outcome = models.CharField(max_length=60, blank=True, help_text="e.g. No Reply, Responded, Approved")
    campaign = models.CharField(max_length=120, blank=True, db_index=True)
    url = models.URLField(max_length=1000, blank=True)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.USER)
    source_id = models.CharField(max_length=255, null=True, blank=True)
    # Email only: RFC 822 Message-ID and the mailbox's thread id, used to spot replies.
    message_id = models.CharField(max_length=500, blank=True, db_index=True)
    thread_id = models.CharField(max_length=100, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "activities"
        ordering = ["-occurred_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["source", "source_id"], name="activity_source_unique"),
        ]
        indexes = [
            models.Index(fields=["company", "-occurred_at"]),
            models.Index(fields=["person", "-occurred_at"]),
        ]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.summary}"


class Attachment(models.Model):
    """A file sent or received on an email. Name and size only; the file stays in Gmail."""

    activity = models.ForeignKey(Activity, on_delete=models.CASCADE, related_name="attachments")
    filename = models.CharField(max_length=300)
    size = models.PositiveIntegerField(default=0, help_text="Approximate bytes")
    content_type = models.CharField(max_length=120, blank=True)
    category = models.CharField(max_length=40, blank=True, db_index=True)

    class Meta:
        ordering = ["-activity__occurred_at", "filename"]
        constraints = [models.UniqueConstraint(fields=["activity", "filename"], name="attachment_unique_per_email")]

    def __str__(self):
        return self.filename


class AISummary(models.Model):
    """Claude's summary of a company or person, kept until someone refreshes it (crm/ai.py)."""

    company = models.OneToOneField(Company, null=True, blank=True, on_delete=models.CASCADE, related_name="ai_summary")
    person = models.OneToOneField(Person, null=True, blank=True, on_delete=models.CASCADE, related_name="ai_summary")
    headline = models.TextField()
    history = models.TextField(blank=True)
    recent = models.JSONField(default=list)
    documents = models.TextField(blank=True)
    next_step = models.TextField(blank=True)
    generated_at = models.DateTimeField()
    last_activity_id = models.PositiveBigIntegerField(default=0, help_text="Newest activity the summary saw")
    model = models.CharField(max_length=80, blank=True)

    class Meta:
        verbose_name = "AI summary"
        verbose_name_plural = "AI summaries"

    def __str__(self):
        return self.headline[:80]


class DuplicateDismissal(models.Model):
    """A group of companies someone marked "not duplicates", so it stops being suggested."""

    key = models.CharField(max_length=500, unique=True)
    company_ids = models.JSONField(default=list)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.key


class Repeat(models.TextChoices):
    NONE = "", "Does not repeat"
    DAILY = "daily", "Every weekday"
    WEEKLY = "weekly", "Every week"
    BIWEEKLY = "biweekly", "Every 2 weeks"
    MONTHLY = "monthly", "Every month"
    QUARTERLY = "quarterly", "Every quarter"


class Task(models.Model):
    title = models.CharField(max_length=300)
    notes = models.TextField(blank=True)
    due_date = models.DateField(null=True, blank=True, db_index=True)
    done = models.BooleanField(default=False, db_index=True)
    done_at = models.DateTimeField(null=True, blank=True)
    assignee = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks")
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.CASCADE, related_name="tasks")
    person = models.ForeignKey(Person, null=True, blank=True, on_delete=models.CASCADE, related_name="tasks")
    opportunity = models.ForeignKey(Opportunity, null=True, blank=True, on_delete=models.CASCADE, related_name="tasks")
    repeat = models.CharField(max_length=10, choices=Repeat.choices, blank=True,
                              help_text="When this is ticked done, the next one is created")
    repeat_until = models.DateField(null=True, blank=True, help_text="Stop repeating after this date")
    meeting = models.ForeignKey("Meeting", null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks",
                                help_text="The meeting whose action item this came from")
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["done", models.F("due_date").asc(nulls_last=True), "-created_at"]

    def __str__(self):
        return self.title

    @property
    def is_overdue(self):
        return not self.done and self.due_date and self.due_date < timezone.localdate()

    def record(self):
        return self.opportunity or self.person or self.company

    def get_absolute_url(self):
        return reverse("crm:task_detail", args=[self.pk])

    def next_due(self):
        """Due date of the next occurrence, or None when it should stop repeating."""
        from datetime import timedelta

        if not self.repeat:
            return None
        base = self.due_date or timezone.localdate()
        if self.repeat == Repeat.DAILY:
            nxt = base + timedelta(days=1)
            while nxt.weekday() >= 5:
                nxt += timedelta(days=1)
        elif self.repeat in (Repeat.WEEKLY, Repeat.BIWEEKLY):
            nxt = base + timedelta(weeks=1 if self.repeat == Repeat.WEEKLY else 2)
        else:
            months = 1 if self.repeat == Repeat.MONTHLY else 3
            y, m = divmod(base.month - 1 + months, 12)
            import calendar

            year, month = base.year + y, m + 1
            nxt = base.replace(year=year, month=month, day=min(base.day, calendar.monthrange(year, month)[1]))
        if self.repeat_until and nxt > self.repeat_until:
            return None
        return nxt


class Meeting(models.Model):
    """One meeting from Granola notes and/or Google Calendar; shown on the Meetings calendar."""

    source = models.CharField(max_length=20, choices=[("granola", "Granola"), ("calendar", "Google Calendar")])
    external_id = models.CharField(max_length=255, unique=True)
    calendar_id = models.CharField(max_length=255, blank=True, help_text="Calendar event merged into this meeting")
    title = models.CharField(max_length=500)
    start = models.DateTimeField(db_index=True)
    end = models.DateTimeField(null=True, blank=True)
    url = models.URLField(max_length=1000, blank=True, help_text="Granola note or calendar event")
    summary = models.TextField(blank=True, help_text="Granola's notes (markdown)")
    action_items = models.JSONField(default=list, blank=True, help_text="[{title, owner, detail, task_id}]")
    attendees = models.JSONField(default=list, blank=True, help_text="[{name, email}]")
    companies = models.ManyToManyField(Company, blank=True, related_name="meetings")
    team = models.ManyToManyField(User, blank=True, related_name="meetings")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start"]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("crm:meeting_list") + f"?month={self.start:%Y-%m}&open={self.pk}"

    @property
    def outside_attendees(self):
        return [a for a in self.attendees if not a.get("email", "").lower().endswith("@acmigroup.com")]


class CalendarSync(models.Model):
    """Per-person Google Calendar sync state (the token itself lives in allauth's SocialToken)."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="calendar_sync")
    enabled = models.BooleanField(default=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    events_seen = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"Calendar sync for {self.user}"


class Comment(models.Model):
    """Trello-style comment on a company, deal or task. @first-name mentions notify that person."""

    body = models.TextField()
    author = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="comments")
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.CASCADE, related_name="comments")
    opportunity = models.ForeignKey(Opportunity, null=True, blank=True, on_delete=models.CASCADE, related_name="comments")
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.CASCADE, related_name="comments")
    mentions = models.ManyToManyField(User, blank=True, related_name="mentioned_in")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.body[:60]

    def record(self):
        return self.task or self.opportunity or self.company


class Notification(models.Model):
    class Kind(models.TextChoices):
        ASSIGNED = "assigned", "Task assigned"
        DUE = "due", "Task due"
        MENTION = "mention", "Mentioned"
        COMMENT = "comment", "Comment"
        REPLY = "reply", "Reply received"
        STAGE = "stage", "Deal stage changed"
        MEETING = "meeting", "Meeting"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    text = models.CharField(max_length=500)
    url = models.CharField(max_length=500, blank=True)
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    key = models.CharField(max_length=120, blank=True, db_index=True, help_text="Stops the same alert twice")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.text


class Note(models.Model):
    body = models.TextField()
    author = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.CASCADE, related_name="notes_set")
    person = models.ForeignKey(Person, null=True, blank=True, on_delete=models.CASCADE, related_name="notes_set")
    opportunity = models.ForeignKey(
        Opportunity, null=True, blank=True, on_delete=models.CASCADE, related_name="notes_set"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.body[:60]


# ---------------------------------------------------------------------------
# Lists and saved views (spec section 5)
# ---------------------------------------------------------------------------


class ObjectType(models.TextChoices):
    COMPANY = "company", "Companies"
    PERSON = "person", "People"
    OPPORTUNITY = "opportunity", "Opportunities"
    LAND = "land", "Land search map"


DEFAULT_LIST_STAGES = ["To review", "In progress", "Done"]


class List(models.Model):
    name = models.CharField(max_length=200, unique=True)
    object_type = models.CharField(max_length=20, choices=ObjectType.choices, default=ObjectType.COMPANY)
    description = models.TextField(blank=True)
    stages = models.JSONField(default=list, blank=True, help_text="Kanban columns for entries in this list")
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.stages:
            self.stages = list(DEFAULT_LIST_STAGES)
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("crm:list_detail", args=[self.pk])


class ListEntry(models.Model):
    list = models.ForeignKey(List, on_delete=models.CASCADE, related_name="entries")
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.CASCADE, related_name="list_entries")
    person = models.ForeignKey(Person, null=True, blank=True, on_delete=models.CASCADE, related_name="list_entries")
    opportunity = models.ForeignKey(
        Opportunity, null=True, blank=True, on_delete=models.CASCADE, related_name="list_entries"
    )
    stage = models.CharField(max_length=80, blank=True)
    custom_fields = models.JSONField(default=dict, blank=True)
    added_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name_plural = "list entries"
        ordering = ["-added_at"]
        constraints = [
            models.UniqueConstraint(fields=["list", "company"], name="listentry_unique_company"),
            models.UniqueConstraint(fields=["list", "person"], name="listentry_unique_person"),
            models.UniqueConstraint(fields=["list", "opportunity"], name="listentry_unique_opportunity"),
        ]

    def __str__(self):
        return f"{self.list}: {self.record()}"

    def record(self):
        return self.company or self.person or self.opportunity


class SavedView(models.Model):
    """A named set of filters/sort/columns, stored as the table's query string."""

    name = models.CharField(max_length=120)
    object_type = models.CharField(max_length=20, choices=ObjectType.choices)
    query = models.TextField(blank=True)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="saved_views")
    shared = models.BooleanField(default=False, help_text="Visible to the whole team")
    is_default = models.BooleanField(default=False, help_text="Opens automatically for its owner (land search map)")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Imported from the workbook; used by Phase 2 outreach
# ---------------------------------------------------------------------------


class EmailTemplate(models.Model):
    """An outreach email. Placeholders: {greeting} {first_name} {company} {sector}."""

    template_id = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=200)
    campaign = models.CharField(max_length=120, blank=True, db_index=True,
                                help_text="Templates in the current campaign are the ones sent")
    priority = models.CharField(max_length=8, choices=Priority.choices, blank=True,
                                help_text="Blank = any priority")
    sector_match = models.CharField(max_length=120, blank=True, help_text="Part of the sector, or * for any")
    subject = models.CharField(max_length=300)
    body_html = models.TextField(blank=True)
    body_plain = models.TextField(blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["template_id"]

    def __str__(self):
        return f"{self.template_id} {self.name}"


class NewsDraft(models.Model):
    """A news-hook email drafted for a company (workbook News_Outreach_Queue)."""

    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.SET_NULL, related_name="news_drafts")
    company_name = models.CharField(max_length=255)
    subject = models.CharField(max_length=300)
    news_hook = models.TextField(blank=True)
    reference_link = models.URLField(max_length=1000, blank=True)
    body_html = models.TextField(blank=True)
    active = models.BooleanField(default=False)
    article_date = models.DateField(null=True, blank=True)
    contact = models.CharField(max_length=255, blank=True)
    source_key = models.CharField(max_length=64, unique=True, editable=False)

    class Meta:
        ordering = ["-article_date"]

    def __str__(self):
        return self.subject


# ---------------------------------------------------------------------------
# Gmail sync (phase 2). See crm/mail/.
# ---------------------------------------------------------------------------


class Mailbox(models.Model):
    """One teammate's Gmail account and where its sync left off.

    Connects over IMAP with a Gmail app password read from the environment
    (ACMI_IMAP_PASSWORD_<USERNAME>, e.g. in .env). Never stored in the database.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="mailbox")
    email = models.EmailField()
    enabled = models.BooleanField(default=True)
    backfill_days = models.PositiveIntegerField(default=365, help_text="How far back the first sync reads")
    backfill_done = models.BooleanField(default=False, editable=False)
    uid_validity = models.CharField(max_length=40, blank=True, editable=False)
    last_uid = models.PositiveBigIntegerField(default=0, editable=False)
    last_synced_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_error = models.TextField(blank=True, editable=False)
    messages_seen = models.PositiveIntegerField(default=0, editable=False)
    # Set while a sync runs so a second sync of the same mailbox waits its turn.
    sync_lock_until = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name_plural = "mailboxes"

    def __str__(self):
        return self.email or str(self.user)

    @property
    def password_env_var(self):
        return f"ACMI_IMAP_PASSWORD_{self.user.username.upper()}"

    def get_password(self):
        import os

        return os.environ.get(self.password_env_var, "").replace(" ", "")


class MailFilter(models.Model):
    """Addresses or domains the sync treats specially.

    protect: never stored or shown (personal contacts, sensitive counterparties).
    ignore: logged if the person already exists, but never suggested as a new company
            (vendors, law firms, newsletters).
    """

    class Kind(models.TextChoices):
        PROTECT = "protect", "Protected (never synced)"
        IGNORE = "ignore", "Ignored (no new companies)"

    pattern = models.CharField(max_length=255, unique=True, help_text="An email address or a domain")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "pattern"]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.pattern}"

    def save(self, *args, **kwargs):
        self.pattern = self.pattern.strip().lower().lstrip("@")
        super().save(*args, **kwargs)


class UnmatchedEmail(models.Model):
    """An email with someone at a domain no company owns yet, waiting for review."""

    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, related_name="unmatched")
    message_id = models.CharField(max_length=500)
    thread_id = models.CharField(max_length=100, blank=True)
    occurred_at = models.DateTimeField()
    kind = models.CharField(max_length=20, choices=Activity.Kind.choices)
    outcome = models.CharField(max_length=60, blank=True)
    address = models.EmailField(max_length=255)
    name = models.CharField(max_length=255, blank=True)
    domain = models.CharField(max_length=255, db_index=True)
    subject = models.CharField(max_length=500, blank=True)
    snippet = models.TextField(blank=True)

    class Meta:
        ordering = ["-occurred_at"]
        constraints = [
            models.UniqueConstraint(fields=["message_id", "address"], name="unmatched_unique_message_address"),
        ]

    def __str__(self):
        return f"{self.address}: {self.subject}"


# ---------------------------------------------------------------------------
# Weekly news review (replaces Desktop/Prospecting/news_review). See crm/news/.
# ---------------------------------------------------------------------------


class NewsScan(models.Model):
    """One weekly prospect news scan file, imported for review."""

    class Status(models.TextChoices):
        OPEN = "open", "In review"
        APPLIED = "applied", "Applied"

    path = models.CharField(max_length=500, unique=True)
    file_name = models.CharField(max_length=255)
    title = models.CharField(max_length=255, blank=True)
    scan_date = models.CharField(max_length=60, blank=True)
    stats_line = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    imported_at = models.DateTimeField(auto_now_add=True)
    imported_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    drafts_checked_at = models.DateTimeField(null=True, blank=True)
    drafts_error = models.TextField(blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    applied_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    results = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-imported_at"]

    def __str__(self):
        return f"{self.title} {self.scan_date}".strip() or self.file_name

    def get_absolute_url(self):
        return reverse("crm:news_review", args=[self.pk])


class NewsItem(models.Model):
    """A company in a scan: a board company with a news hook, or a new prospect."""

    class Kind(models.TextChoices):
        BOARD = "board", "Board company"
        PROSPECT = "prospect", "New prospect"
        WEAK = "weak", "Also surfaced"

    class Decision(models.TextChoices):
        NONE = "", "Undecided"
        PROCEED = "proceed", "Proceed"
        DECLINE = "decline", "Decline"
        PURSUE = "pursue", "Pursue"
        PASS = "pass", "Pass"

    scan = models.ForeignKey(NewsScan, on_delete=models.CASCADE, related_name="items")
    kind = models.CharField(max_length=10, choices=Kind.choices)
    position = models.PositiveIntegerField(default=0)
    section = models.CharField(max_length=120, blank=True)
    tier = models.CharField(max_length=20, blank=True, help_text="strong / watching (prospects)")
    company_name = models.CharField(max_length=255)
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.SET_NULL, related_name="news_items")
    headline = models.TextField(blank=True)
    article_date = models.CharField(max_length=60, blank=True)
    source_url = models.URLField(max_length=1000, blank=True)
    summary = models.TextField(blank=True)
    outreach_action = models.TextField(blank=True)
    framing = models.TextField(blank=True)
    contact_text = models.CharField(max_length=500, blank=True)
    needs_contact = models.BooleanField(default=False)
    has_draft = models.BooleanField(default=False)
    hq = models.CharField(max_length=255, blank=True)
    what_they_do = models.TextField(blank=True)
    why_fits = models.TextField(blank=True)
    source_name = models.CharField(max_length=255, blank=True)

    decision = models.CharField(max_length=10, choices=Decision.choices, blank=True)
    note = models.TextField(blank=True)
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    draft_uid = models.CharField(max_length=40, blank=True)
    draft_score = models.FloatField(default=0)
    draft_to = models.TextField(blank=True)
    draft_cc = models.TextField(blank=True)
    draft_subject = models.CharField(max_length=500, blank=True)
    draft_html = models.TextField(blank=True)
    draft_snippet = models.TextField(blank=True)
    draft_too_large = models.BooleanField(default=False)
    edit_to = models.TextField(blank=True)
    edit_subject = models.CharField(max_length=500, blank=True)
    edit_html = models.TextField(blank=True)
    edited = models.BooleanField(default=False)

    result = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["kind", "position"]

    def __str__(self):
        return f"{self.company_name}: {self.headline[:60]}"

    @property
    def to(self):
        return self.edit_to if self.edited else self.draft_to

    @property
    def subject(self):
        return self.edit_subject if self.edited else self.draft_subject

    @property
    def html(self):
        return self.edit_html if self.edited else self.draft_html


# ---------------------------------------------------------------------------
# Outreach sending (replaces send_emails_v4.py) and the weekly report. See crm/outreach/.
# ---------------------------------------------------------------------------


class OutreachSettings(models.Model):
    """One row: the rules that used to live on the workbook's Settings sheet."""

    current_campaign = models.CharField(max_length=120, default="September v1")
    cooldown_days = models.PositiveIntegerField(default=7, help_text="Days since the last email before emailing again")
    max_touches = models.PositiveIntegerField(default=15, help_text="Stop after this many outreach emails")
    include_high = models.BooleanField(default=True)
    include_mid = models.BooleanField(default=True)
    include_low = models.BooleanField(default=True)
    include_test = models.BooleanField(default=False)
    exclude_responded = models.BooleanField(default=True)
    exclude_not_interested = models.BooleanField(default=True)
    exclude_undeliverable = models.BooleanField(default=True)
    exclude_tbd_names = models.BooleanField(default=False, help_text="Skip people with no known name")
    owner_filter = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                                     help_text="Only this owner's people (blank = everyone)")
    prioritize_news = models.BooleanField(
        default=False, help_text="Skip companies with news outreach in the last 30 days (they get the news email instead)"
    )
    skip_late_stages = models.BooleanField(
        default=True, help_text="Skip companies at Opportunity, Tenant, Not interested or Do not contact"
    )
    skip_recent_replies_days = models.PositiveIntegerField(
        default=30, help_text="Skip anyone who replied within this many days (0 = off)"
    )
    include_attachments = models.BooleanField(default=True)
    delay_seconds = models.PositiveIntegerField(default=5, help_text="Pause between emails")
    daily_cap = models.PositiveIntegerField(default=500, help_text="Most emails one person may send per day")
    sender_name = models.CharField(max_length=120, default="Kellen Moczulski")
    sender_title = models.CharField(max_length=120, default="Real Estate Associate")
    sender_phone = models.CharField(max_length=40, default="(817)-709-7565")
    signature_logo_url = models.URLField(blank=True, default="https://imgur.com/cyXkuLf.png")
    report_recipients = models.TextField(
        blank=True, default="sshewmaker@acmigroup.com, ddowell@acmigroup.com, ddenardo@acmigroup.com",
        help_text="Who gets the weekly report (comma-separated)",
    )

    class Meta:
        verbose_name = verbose_name_plural = "outreach settings"

    def __str__(self):
        return "Outreach settings"

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class OutreachFile(models.Model):
    """A file attached to every outreach email (brochures, flyers)."""

    name = models.CharField(max_length=200)
    file = models.FileField(upload_to="outreach/")
    active = models.BooleanField(default=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "name"]

    def __str__(self):
        return self.name


class SendBatch(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENDING = "sending", "Sending"
        DONE = "done", "Done"
        STOPPED = "stopped", "Stopped"
        FAILED = "failed", "Failed"

    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="send_batches")
    created_at = models.DateTimeField(auto_now_add=True)
    campaign = models.CharField(max_length=120)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    stop_requested = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.campaign} · {self.created_at:%b %d %H:%M}"

    def get_absolute_url(self):
        return reverse("crm:send_batch", args=[self.pk])

    def counts(self):
        from collections import Counter

        return Counter(self.items.values_list("status", flat=True))


class SendItem(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        # Claimed by a sender just before it goes to Gmail. One left here after a crash may or may not have gone
        # out, so it is never sent again automatically.
        SENDING = "sending", "Sending (unconfirmed)"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        SKIPPED = "skipped", "Skipped"

    batch = models.ForeignKey(SendBatch, on_delete=models.CASCADE, related_name="items")
    person = models.ForeignKey(Person, null=True, on_delete=models.SET_NULL, related_name="+")
    template = models.ForeignKey(EmailTemplate, null=True, on_delete=models.SET_NULL, related_name="+")
    email = models.EmailField(max_length=255)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    error = models.TextField(blank=True)
    message_id = models.CharField(max_length=500, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["pk"]


# ---------------------------------------------------------------------------
# Land search (phase 4). See crm/land/.
# ---------------------------------------------------------------------------


class LandCounty(models.Model):
    """A county whose parcels have been loaded from the state's parcel release."""

    fips = models.CharField(max_length=5, unique=True, help_text="State + county FIPS, e.g. 48209")
    name = models.CharField(max_length=80)
    state = models.CharField(max_length=2, default="TX")
    region = models.CharField(max_length=60, blank=True, help_text="e.g. Austin–San Antonio, Southern Maryland")
    source = models.CharField(max_length=200, blank=True, help_text="Appraisal district / publisher")
    release = models.CharField(max_length=40, blank=True, help_text="e.g. TxGIO 2026-09-25")
    tax_year = models.CharField(max_length=4, blank=True)
    total_parcels = models.PositiveIntegerField(default=0)
    kept_parcels = models.PositiveIntegerField(default=0)
    min_acres = models.DecimalField(max_digits=7, decimal_places=2, default=20)
    loaded_at = models.DateTimeField(null=True, blank=True)
    cad_search_url = models.URLField(max_length=300, blank=True,
                                     help_text="Appraisal district property search, {prop_id} is filled in")

    class Meta:
        ordering = ["state", "region", "name"]
        verbose_name_plural = "land counties"

    def __str__(self):
        return f"{self.name} County, {self.state}"

    @property
    def state_name(self):
        return {"TX": "Texas", "MD": "Maryland", "VA": "Virginia", "AL": "Alabama", "NC": "North Carolina", "AZ": "Arizona", "IN": "Indiana"}.get(self.state, self.state)


class Parcel(models.Model):
    class Status(models.TextChoices):
        NEW = "", "Not reviewed"
        WATCH = "watch", "Watch"
        PURSUE = "pursue", "Pursue"
        PASS = "pass", "Pass"

    county = models.ForeignKey(LandCounty, on_delete=models.CASCADE, related_name="parcels")
    key = models.CharField(max_length=80, help_text="Appraisal district property id (or geo id)")
    prop_id = models.CharField(max_length=40, blank=True)
    geo_id = models.CharField(max_length=60, blank=True)
    owner_name = models.CharField(max_length=255, blank=True, db_index=True)
    care_of = models.CharField(max_length=255, blank=True)
    mail_address = models.CharField(max_length=300, blank=True)
    mail_city = models.CharField(max_length=80, blank=True)
    mail_state = models.CharField(max_length=2, blank=True)
    mail_zip = models.CharField(max_length=10, blank=True)
    situs_address = models.CharField(max_length=300, blank=True)
    situs_city = models.CharField(max_length=80, blank=True)
    situs_zip = models.CharField(max_length=10, blank=True)
    legal_desc = models.CharField(max_length=300, blank=True)
    acres = models.FloatField(db_index=True, help_text="Mapped (GIS) acres")
    legal_acres = models.FloatField(null=True, blank=True)
    land_value = models.BigIntegerField(null=True, blank=True)
    improvement_value = models.BigIntegerField(null=True, blank=True)
    market_value = models.BigIntegerField(null=True, blank=True)
    state_land_use = models.CharField(max_length=20, blank=True, help_text="Texas PTAD category, e.g. D1, E1, F2")
    local_land_use = models.CharField(max_length=20, blank=True)
    year_built = models.CharField(max_length=60, blank=True)
    tax_year = models.CharField(max_length=4, blank=True)
    # Geometry: WGS84 GeoJSON (simplified a few metres) plus centroid and bounding box for map queries.
    geometry = models.TextField()
    lat = models.FloatField(db_index=True)
    lon = models.FloatField(db_index=True)
    min_lat = models.FloatField()
    min_lon = models.FloatField()
    max_lat = models.FloatField()
    max_lon = models.FloatField()
    # Computed from stored layers (crm/land/layers.py), in miles.
    in_city = models.CharField(max_length=80, blank=True, help_text="City whose limits it lies in; blank = unincorporated")
    zoning = models.CharField(max_length=120, blank=True, help_text="County zoning where the county publishes it (e.g. IG)")
    zoning_class = models.CharField(max_length=20, blank=True, db_index=True,
                                    help_text="Standard bucket across counties: industrial, business_park, mixed_use...")
    overlays = models.JSONField(default=dict, blank=True,
                                help_text="County planning layers: water/sewer category, critical area, military zones...")
    near = models.JSONField(default=dict, blank=True, help_text="{layer: {miles, name, ...}}")
    owner_acres = models.FloatField(null=True, blank=True, help_text="Same owner's total acres in this county")
    owner_parcels = models.PositiveIntegerField(null=True, blank=True)
    # Live lookups (crm/land/lookup.py), run on demand and cached here.
    checks = models.JSONField(default=dict, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)
    # Buy-box screen (crm/land/score.py).
    score = models.PositiveSmallIntegerField(default=0, db_index=True)
    score_reasons = models.JSONField(default=list, blank=True)
    killers = models.JSONField(default=list, blank=True)
    # Team review.
    status = models.CharField(max_length=10, choices=Status.choices, blank=True, default="", db_index=True)
    status_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    status_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    qd_design = models.JSONField(default=dict, blank=True, help_text="QD site layout: buildings and positions")
    site_plan = models.JSONField(default=dict, blank=True, help_text="Quick site plan inputs (zone, overrides, program)")
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.SET_NULL, related_name="parcels",
                                help_text="The landowner, once someone chose Pursue")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-score", "-acres"]
        constraints = [models.UniqueConstraint(fields=["county", "key"], name="parcel_county_key_unique")]
        indexes = [models.Index(fields=["lat", "lon"])]

    def __str__(self):
        return f"{self.acres:,.0f} ac · {self.owner_name or 'Unknown owner'} ({self.county.name})"

    def get_absolute_url(self):
        return reverse("crm:land_search") + f"?parcel={self.pk}"

    @property
    def label(self):
        place = self.situs_address or self.legal_desc or f"Property {self.prop_id}"
        return f"{self.acres:,.0f} ac · {place}"

    @property
    def value_per_acre(self):
        if self.market_value and self.acres:
            return round(self.market_value / self.acres)
        return None

    @property
    def mailing(self):
        city = ", ".join(x for x in [self.mail_city, " ".join(x for x in [self.mail_state, self.mail_zip] if x)] if x)
        return " · ".join(x for x in [self.mail_address, city] if x)

    @property
    def cad_url(self):
        """Appraisal district / Maryland SDAT record. SDAT ids are county(2) + district(2) + account."""
        url = self.county.cad_search_url
        if not (url and self.prop_id):
            return ""
        if "{a}" in url:
            pid = self.prop_id
            return url.format(c=pid[:2], d=pid[2:4], a=pid[4:])
        return url.replace("{prop_id}", self.prop_id)


class LandFeature(models.Model):
    """A stored map feature used for distance screening (bases, power lines, substations, rail, highways, city limits)."""

    layer = models.CharField(max_length=30, db_index=True)
    name = models.CharField(max_length=255, blank=True)
    attrs = models.JSONField(default=dict, blank=True)
    geometry = models.TextField(help_text="WGS84 GeoJSON geometry")
    min_lat = models.FloatField()
    min_lon = models.FloatField()
    max_lat = models.FloatField()
    max_lon = models.FloatField()

    class Meta:
        indexes = [models.Index(fields=["layer", "min_lat", "min_lon"])]

    def __str__(self):
        return f"{self.layer}: {self.name}"


class LandSite(models.Model):
    """A prospective site: one parcel or an assemblage of several, saved for a campus and/or project."""

    class Status(models.TextChoices):
        PROSPECT = "prospect", "Prospect"
        REVIEW = "review", "Under review"
        OUTREACH = "outreach", "Owner outreach"
        LOI = "loi", "LOI / negotiating"
        CONTRACT = "contract", "Under contract"
        ACQUIRED = "acquired", "Acquired"
        DROPPED = "dropped", "Dropped"

    name = models.CharField(max_length=200)
    campus = models.ForeignKey(Campus, null=True, blank=True, on_delete=models.SET_NULL, related_name="land_sites")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="land_sites")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PROSPECT, db_index=True)
    parcels = models.ManyToManyField(Parcel, blank=True, related_name="sites")
    notes = models.TextField(blank=True)
    qd_design = models.JSONField(default=dict, blank=True, help_text="QD site layout on the assembled boundary")
    milestones = models.JSONField(default=dict, blank=True, help_text="{milestone key: 'YYYY-MM-DD'} (see MILESTONES)")
    site_plan = models.JSONField(default=dict, blank=True, help_text="Quick site plan inputs (zone, overrides, program)")
    listing = models.JSONField(default=dict, blank=True, help_text="Facts from the broker flyer or listing (see crm/land/briefs.py LISTING_KEYS)")
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["campus__code", "project__name", "name"]

    def __str__(self):
        return self.name

    # Furthest along first; dropped last.
    STATUS_ORDER = ["acquired", "contract", "loi", "outreach", "review", "prospect", "dropped"]

    # Deal timeline, in order. Reaching a milestone moves the status forward to the one shown (never back).
    MILESTONES = [
        ("identified", "Identified", "prospect"),
        ("contacted", "Owner contacted", "outreach"),
        ("loi_sent", "LOI sent", "loi"),
        ("loi_signed", "LOI signed", "loi"),
        ("psa_sent", "PSA drafted / sent", "loi"),
        ("psa_signed", "PSA signed", "contract"),
        ("feasibility_end", "Feasibility ends", "contract"),
        ("approvals", "Approvals / entitlements", "contract"),
        ("closed", "Closed", "acquired"),
    ]

    def milestone_rows(self):
        """[(key, label, date or None, days since the previous reached milestone)] in timeline order."""
        from datetime import date

        rows, prev, first = [], None, None
        for key, label, _ in self.MILESTONES:
            raw = (self.milestones or {}).get(key)
            when = date.fromisoformat(raw) if raw else None
            gap = (when - prev).days if when and prev else None
            if when and first is None:
                first = when
            total = (when - first).days if when and first else None
            rows.append({"key": key, "label": label, "date": when, "gap": gap, "total": total})
            if when:
                prev = when
        return rows

    def timeline_summary(self):
        """Overall clock: first dated milestone to closing (or today while still open). None if no dates."""
        from datetime import date

        dated = [r for r in self.milestone_rows() if r["date"]]
        if not dated:
            return None
        first, last = dated[0], dated[-1]
        closed = last["key"] == "closed"
        end = last["date"] if closed else date.today()
        return {"start": first["date"], "start_label": first["label"], "end": end, "closed": closed,
                "days": (end - first["date"]).days, "last_label": last["label"], "last_date": last["date"],
                "since_last": (date.today() - last["date"]).days if not closed else 0,
                "ahead": max(0, (last["date"] - date.today()).days)}

    def status_for_milestones(self):
        """The furthest status the recorded milestones imply (None if none recorded)."""
        reached = [st for key, _, st in self.MILESTONES if (self.milestones or {}).get(key)]
        if not reached:
            return None
        return min(reached, key=lambda st: self.STATUS_ORDER.index(st))

    @classmethod
    def by_status(cls, sites):
        """Sort sites by deal stage, then name."""
        return sorted(sites, key=lambda s: (cls.STATUS_ORDER.index(s.status) if s.status in cls.STATUS_ORDER
                                            else len(cls.STATUS_ORDER), s.name.lower()))

    def get_absolute_url(self):
        return reverse("crm:land_search") + f"?site={self.pk}"

    @property
    def acres(self):
        return round(sum(p.acres for p in self.parcels.all()), 1)

    @property
    def home_label(self):
        return " / ".join(str(x) for x in [self.campus, self.project] if x) or "No campus or project"


class LandSiteEvent(models.Model):
    """Change log for a land site: status moves, milestones, notes. Feeds the site's timeline."""

    site = models.ForeignKey(LandSite, on_delete=models.CASCADE, related_name="events")
    when = models.DateTimeField(default=timezone.now, db_index=True)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=20, help_text="status, milestone, note, edit")
    text = models.CharField(max_length=500)

    class Meta:
        ordering = ["-when", "-id"]

    def __str__(self):
        return self.text[:60]


class SiteParcelDeal(models.Model):
    """One parcel's own deal inside a site (an assemblage closes owner by owner): status, milestones, notes."""

    site = models.ForeignKey(LandSite, on_delete=models.CASCADE, related_name="parcel_deals")
    parcel = models.ForeignKey(Parcel, on_delete=models.CASCADE, related_name="site_deals")
    status = models.CharField(max_length=10, choices=LandSite.Status.choices, default=LandSite.Status.REVIEW)
    milestones = models.JSONField(default=dict, blank=True, help_text="{milestone key: 'YYYY-MM-DD'}")
    notes = models.TextField(blank=True)
    # Excluded parcels stay on the site (history, owner contact) but drop out of its acres, outline, QD and plan.
    included = models.BooleanField(default=True)
    # The part of the parcel this site needs (GeoJSON, WGS84), e.g. only the side of a trail that touches the
    # rest of the assemblage. Blank = the whole parcel.
    portion = models.TextField(blank=True)
    portion_acres = models.FloatField(null=True, blank=True)
    portion_cuts = models.JSONField(default=list, blank=True, help_text="Cut lines drawn to make the portion")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["site", "parcel"], name="site_parcel_deal_unique")]

    def __str__(self):
        return f"{self.site} · {self.parcel.prop_id or self.parcel.key}"

    @property
    def acres(self):
        """Acres this site counts for the parcel: the trimmed portion when there is one."""
        return self.portion_acres if self.portion and self.portion_acres is not None else self.parcel.acres

    @property
    def trimmed(self):
        return bool(self.portion)

    def geometry_json(self):
        return self.portion or self.parcel.geometry

    def milestone_rows(self):
        from datetime import date

        return [{"key": k, "label": label, "date": date.fromisoformat(self.milestones[k]) if (self.milestones or {}).get(k) else None}
                for k, label, _ in LandSite.MILESTONES]

    def status_for_milestones(self):
        reached = [st for key, _, st in LandSite.MILESTONES if (self.milestones or {}).get(key)]
        return min(reached, key=lambda st: LandSite.STATUS_ORDER.index(st)) if reached else None


class Assignment(models.Model):
    """A land search for a client: many candidate sites, each with a diligence report, ranked against each other.

    Clients see it through a private share link (crm/land/assignments.py, the /g/<token>/ pages).
    """

    name = models.CharField(max_length=200, help_text="e.g. Elbit 26 Defense land search")
    client = models.CharField(max_length=200, blank=True)
    program = models.CharField(max_length=300, blank=True, help_text="End use, e.g. energetics manufacturing campus")
    demand_driver = models.CharField(max_length=200, blank=True, help_text="Installation or gate the program serves")
    acreage_floor = models.DecimalField(max_digits=10, decimal_places=1, null=True, blank=True,
                                        help_text="Net usable acres the program needs")
    guest_intro = models.TextField(blank=True, help_text="Cover note shown to the client above the ranking")
    notes = models.TextField(blank=True, help_text="Internal only")
    share_token = models.CharField(max_length=64, blank=True, db_index=True)
    share_enabled = models.BooleanField(default=False)
    share_expires = models.DateField(null=True, blank=True)
    guest_views = models.PositiveIntegerField(default=0)
    guest_last_view = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("crm:assignment_detail", args=[self.pk])

    def share_active(self):
        # The team's date (TIME_ZONE), not the server's: a Cloud Run server on UTC would end links hours early.
        return bool(self.share_enabled and self.share_token
                    and (not self.share_expires or self.share_expires >= timezone.localdate()))


class AssignmentSite(models.Model):
    """A site inside an assignment, with the diligence report written for that client."""

    assignment = models.ForeignKey(Assignment, on_delete=models.CASCADE, related_name="entries")
    site = models.ForeignKey(LandSite, on_delete=models.CASCADE, related_name="assignment_entries")
    report = models.JSONField(default=dict, blank=True, help_text="Ratings, findings, checklist, utilities, permits, ROM")
    rank_override = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Pin to this position in the ranking")
    show_to_guest = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["assignment", "site"], name="assignment_site_unique")]

    def __str__(self):
        return f"{self.assignment} · {self.site}"
