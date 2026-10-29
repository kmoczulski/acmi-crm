"""ACMI CRM data model. See docs/attio_spec.md, sections 1, 4 and 5."""
from contextvars import ContextVar
from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from .choices import Group, Lookup, Order
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
STAGE_PROBABILITY = Lookup("OpportunityStage", {
    OpportunityStage.PROSPECT: 10,
    OpportunityStage.QUALIFIED: 25,
    OpportunityStage.LOI_NEGOTIATION: 50,
    OpportunityStage.LOI_SIGNED: 75,
    OpportunityStage.LEASE_NEGOTIATION: 85,
    OpportunityStage.SIGNED: 100,
    OpportunityStage.ON_HOLD: 5,
    OpportunityStage.LOST: 0,
})
OPEN_STAGES = Group("OpportunityStage", [
    OpportunityStage.PROSPECT, OpportunityStage.QUALIFIED, OpportunityStage.LOI_NEGOTIATION,
    OpportunityStage.LOI_SIGNED, OpportunityStage.LEASE_NEGOTIATION,
])
STAGE_ORDER = Lookup("OpportunityStage", {value: i for i, value in enumerate(OpportunityStage.values)})


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
    drive_folder = models.CharField("Drive folder", max_length=100, blank=True,
                                    help_text="The project's Google Drive folder (its ID), for the Drive files panel")
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
    drive_folder = models.CharField("Drive folder", max_length=100, blank=True,
                                    help_text="The company's tenancy folder in Google Drive (its ID)")
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
    partner_note = models.TextField(
        "note for capital partners", blank=True,
        help_text="Shown to capital partners in the data room. Write it for outsiders: never internal notes.",
    )
    people = models.ManyToManyField(Person, blank=True, related_name="opportunities", through="OpportunityContact")
    land_sites = models.ManyToManyField("LandSite", blank=True, related_name="tenant_deals",
                                        help_text="The land sites this tenant is considering or committed to")
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
    class Label(models.TextChoices):  # the categories the team's Trello boards use
        NONE = "", "No label"
        DEAL = "deal", "Deal info"
        LEGAL = "legal", "Legal"
        CIVIL = "civil", "Civil"
        ARCHITECTURE = "architecture", "Architecture"
        PROGRAM = "program", "Program / procurement"
        LAND_USE = "land_use", "Land use"
        CRITICAL = "critical", "Critical date"

    title = models.CharField(max_length=300)
    label = models.CharField(max_length=12, choices=Label.choices, blank=True)
    notes = models.TextField(blank=True)
    due_date = models.DateField(null=True, blank=True, db_index=True)
    due_time = models.TimeField(null=True, blank=True, help_text="Optional time on the due date; you get a reminder then")
    done = models.BooleanField(default=False, db_index=True)
    done_at = models.DateTimeField(null=True, blank=True)
    assignee = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="tasks")
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.CASCADE, related_name="tasks")
    person = models.ForeignKey(Person, null=True, blank=True, on_delete=models.CASCADE, related_name="tasks")
    opportunity = models.ForeignKey(Opportunity, null=True, blank=True, on_delete=models.CASCADE, related_name="tasks")
    site = models.ForeignKey("LandSite", null=True, blank=True, on_delete=models.CASCADE, related_name="tasks",
                             help_text="The land site this task is for (the site card's checklist)")
    repeat = models.CharField(max_length=10, choices=Repeat.choices, blank=True,
                              help_text="When this is ticked done, the next one is created")
    repeat_until = models.DateField(null=True, blank=True, help_text="Stop repeating after this date")
    meetings = models.ManyToManyField("Meeting", blank=True, related_name="tasks",
                                      help_text="The meetings whose action item this came from (the same item on two "
                                                "meetings is one task)")
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["done", models.F("due_date").asc(nulls_last=True), "-created_at"]

    def __str__(self):
        return self.title

    @property
    def is_overdue(self):
        if self.done or not self.due_date:
            return False
        today = timezone.localdate()
        if self.due_date < today:
            return True
        return bool(self.due_date == today and self.due_time and self.due_time < timezone.localtime().time())

    @property
    def due_at(self):
        """The due date and time as an aware datetime (None without a time)."""
        from datetime import datetime

        if not (self.due_date and self.due_time):
            return None
        return timezone.make_aware(datetime.combine(self.due_date, self.due_time))

    def record(self):
        return self.opportunity or self.person or self.company or self.site

    @property
    def meeting(self):
        """The first meeting the task came from (most have one)."""
        return min(self.meetings.all(), key=lambda m: m.start, default=None)

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

    source = models.CharField(max_length=20, choices=[("granola", "Granola"), ("calendar", "Google Calendar"),
                                                      ("invite", "Emailed invite")])
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
    firms = models.ManyToManyField("Firm", blank=True, related_name="meetings",
                                   help_text="Directory firms at the meeting, by attendee domain or contact email")
    team = models.ManyToManyField(User, blank=True, related_name="meetings")
    # Outside attendees no record knows (no person, company or firm, not free mail, not ignored): the calendar's
    # "no record" marker and the Needs filing cards. Kept up to date by crm/needs_filing.py.
    unfiled = models.JSONField(default=list, blank=True, help_text="[{name, email}] of outside attendees with no record")
    unfiled_domains = models.JSONField(default=list, blank=True)
    invite_version = models.CharField(max_length=40, blank=True,
                                      help_text="An emailed invite's SEQUENCE and DTSTAMP: an older copy never wins")
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

    @property
    def unfiled_emails(self):
        return {a["email"].lower() for a in self.unfiled or []}


class CampusUpdate(models.Model):
    """A one-line update on a campus workstream item: what happened off-system (a call, a site visit), who the ball is
    with, and when something was promised. Feeds the campus Summary tab and the weekly campus report (crm/campus.py)."""

    class Workstream(models.TextChoices):
        TENANT = "tenant", "Tenants"
        LAND = "land", "Land"
        PROJECT = "project", "Projects"
        OTHER = "other", "Other"

    class Waiting(models.TextChoices):
        NONE = "", "Nobody"
        US = "us", "Waiting on us"
        THEM = "them", "Waiting on them"
        THIRD = "third", "Waiting on a third party"

    campus = models.ForeignKey(Campus, on_delete=models.CASCADE, related_name="updates")
    workstream = models.CharField(max_length=10, choices=Workstream.choices, default=Workstream.OTHER)
    opportunity = models.ForeignKey("Opportunity", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="campus_updates")
    site = models.ForeignKey("LandSite", null=True, blank=True, on_delete=models.SET_NULL, related_name="campus_updates")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="campus_updates")
    text = models.TextField()
    waiting_on = models.CharField(max_length=6, choices=Waiting.choices, blank=True)
    promised_on = models.DateField("promised for", null=True, blank=True,
                                   help_text="When the thing we're waiting for was promised (redlines, a reply)")
    happened_on = models.DateField(default=timezone.localdate)
    author = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-happened_on", "-created_at"]

    def __str__(self):
        return self.text[:60]


class CampusReport(models.Model):
    """The weekly campus update: drafted by Claude from the Summary tab (crm/campus_ai.py), edited by the team,
    then sent by a manager from their own Gmail. One per campus per week; sent ones are kept as the archive."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SENT = "sent", "Sent"

    campus = models.ForeignKey(Campus, on_delete=models.CASCADE, related_name="reports")
    week_of = models.DateField(help_text="Monday of the week the update covers")
    body = models.TextField(help_text="The update as it goes out (plain text, bullet lines)")
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.DRAFT)
    model = models.CharField(max_length=60, blank=True, help_text="Claude model that wrote the draft")
    generated_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    recipients = models.TextField(blank=True, help_text="Comma-separated addresses it went (or goes) to")
    sent_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    sent_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-week_of"]
        constraints = [models.UniqueConstraint(fields=["campus", "week_of"], name="one_campus_report_per_week")]

    def __str__(self):
        return f"{self.campus.code} week of {self.week_of:%b %d}"


class MeetingTranscript(models.Model):
    """A meeting's full transcript, kept for good: Granola's free plan only shows the last 30 days. Each one is
    also written out as a .txt file (crm/transcripts.py, settings.TRANSCRIPT_DIR). Managers only."""

    meeting = models.OneToOneField(Meeting, on_delete=models.CASCADE, related_name="transcript")
    text = models.TextField()
    source = models.CharField(max_length=20, default="granola")
    words = models.PositiveIntegerField(default=0)
    fetched_at = models.DateTimeField(auto_now=True)
    file_path = models.CharField(max_length=500, blank=True, help_text="Where the .txt copy was last written")

    def __str__(self):
        return f"Transcript: {self.meeting}"


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
    """Trello-style comment on a company, deal, task or land site. @first-name mentions notify that person."""

    body = models.TextField()
    author = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="comments")
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.CASCADE, related_name="comments")
    opportunity = models.ForeignKey(Opportunity, null=True, blank=True, on_delete=models.CASCADE, related_name="comments")
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.CASCADE, related_name="comments")
    site = models.ForeignKey("LandSite", null=True, blank=True, on_delete=models.CASCADE, related_name="comments")
    contract = models.ForeignKey("Contract", null=True, blank=True, on_delete=models.CASCADE, related_name="comments")
    card = models.ForeignKey("Card", null=True, blank=True, on_delete=models.CASCADE, related_name="comments",
                             help_text="A board card (crm/boards)")
    document = models.ForeignKey("ContractDocument", null=True, blank=True, on_delete=models.CASCADE,
                                 related_name="comments", help_text="The contract document it's pinned to")
    start = models.PositiveIntegerField(null=True, blank=True, help_text="Where in the document's text it's pinned")
    end = models.PositiveIntegerField(null=True, blank=True)
    quote = models.TextField(blank=True, help_text="The words it's pinned to")
    parent = models.ForeignKey("self", null=True, blank=True, on_delete=models.CASCADE, related_name="replies")
    resolved = models.BooleanField(default=False)
    by_claude = models.BooleanField(default=False, help_text="Claude's note on a change in a redline")
    mentions = models.ManyToManyField(User, blank=True, related_name="mentioned_in")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.body[:60]

    def record(self):
        return self.card or self.task or self.opportunity or self.site or self.contract or self.company

    def link(self):
        """Where the comment opens: a contract comment on its document in the reader, anything else on its record."""
        if self.document_id:
            root = self.parent or self
            return reverse("crm:contract_reader", args=[self.contract_id, self.document_id]) + f"#c-{root.pk}"
        return self.record().get_absolute_url() + "#comments"


class Notification(models.Model):
    class Kind(models.TextChoices):
        ASSIGNED = "assigned", "Task assigned"
        DUE = "due", "Task due"
        MENTION = "mention", "Mentioned"
        COMMENT = "comment", "Comment"
        REPLY = "reply", "Reply received"
        STAGE = "stage", "Deal stage changed"
        MEETING = "meeting", "Meeting"
        DEADLINE = "deadline", "Deadline"
        DIRECTORY = "directory", "Filed in the directory"
        CARD = "card", "Card"
        MAIL = "mail", "New email"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    text = models.CharField(max_length=500)
    url = models.CharField(max_length=500, blank=True)
    actor = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    key = models.CharField(max_length=120, blank=True, db_index=True, help_text="Stops the same alert twice")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    # False when the person turned this kind off "in the hub" (crm/push.py): kept only so its key stops repeats.
    in_hub = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # Two page loads at once both ran the daily due sweep and sent the same alert twice.
            models.UniqueConstraint(fields=["user", "key"], condition=~models.Q(key=""), name="notification_once_per_key"),
        ]

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
    # An ObjectType for the Companies / People tables and the land map, or a page key ("tasks", "pipeline"...)
    # for the shared view bar on every other list (crm/prefs.py VIEW_PAGES).
    object_type = models.CharField(max_length=40)
    query = models.TextField(blank=True)
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="saved_views")
    shared = models.BooleanField(default=False, help_text="Visible to the whole team")
    is_default = models.BooleanField(default=False, help_text="Opens automatically for its owner (land search map)")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ImportBatch(models.Model):
    """One spreadsheet import (crm/sheet_import/): who ran it, what each row did, and what Undo has to put back.

    `undo` holds {"companies": [pk], "people": [pk], "company_changes": {pk: {field: [old, new]}},
    "person_changes": {...}, "added_domains": {pk: [domain]}, "added_phones": {pk: [phone]}, "list_entries": [pk],
    "lists": [pk], "activities": [pk]}. The uploaded file itself is never kept."""

    class Mode(models.TextChoices):
        FILL = "fill", "Fill blanks only"
        UPDATE = "update", "Update with the sheet's values"

    filename = models.CharField(max_length=255)
    sheet = models.CharField(max_length=120, blank=True)
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.FILL)
    rows_read = models.PositiveIntegerField(default=0)
    counts = models.JSONField(default=dict, blank=True)
    results = models.JSONField(default=list, blank=True, help_text="What each row did, for the results page")
    undo = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    undone_at = models.DateTimeField(null=True, blank=True)
    undone_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "import batches"

    def __str__(self):
        return f"{self.filename} ({self.created_at:%Y-%m-%d})"

    def get_absolute_url(self):
        return reverse("crm:sheet_import_result", args=[self.pk])


class PushSubscription(models.Model):
    """One browser on one device that turned on browser notifications (Web Push, crm/push.py)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="push_subscriptions")
    endpoint = models.URLField(max_length=1000, unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    device = models.CharField(max_length=120, blank=True, help_text="Browser and system, from the user agent")
    created_at = models.DateTimeField(auto_now_add=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} on {self.device or 'a browser'}"


class UserPrefs(models.Model):
    """One person's layout choices: dashboard widgets, hidden table columns, density (see crm/prefs.py)."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="prefs")
    data = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "user preferences"
        verbose_name_plural = "user preferences"

    def __str__(self):
        return f"Preferences for {self.user}"


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
    body_source = models.TextField(blank=True, help_text="Plain-text body written in the editor; when set it generates body_html and body_plain (see crm/outreach/richtext.py)")
    body_html = models.TextField(blank=True)
    body_plain = models.TextField(blank=True)
    attachment = models.FileField(upload_to="outreach/templates/", blank=True, help_text="A PDF sent with this template's emails, on top of the attachments every email gets")
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
    """One teammate's Gmail account and where its sync left off, or a shared address (shared=True, no user).

    A teammate's connects over IMAP with a Gmail app password read from the environment
    (ACMI_IMAP_PASSWORD_<USERNAME>, e.g. in .env), never stored in the database, or through the Gmail API once they
    turn Mail in the hub on. A shared address (deals@) is connected once by a Manager signing in as it, and read
    through its own token (SharedMailboxToken) by the teammates it's granted to (MailboxAccess).
    """

    user = models.OneToOneField(User, null=True, blank=True, on_delete=models.CASCADE, related_name="mailbox")
    shared = models.BooleanField(default=False)
    name = models.CharField(max_length=120, blank=True, help_text="A shared mailbox's name in the hub, e.g. Deals")
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

    class Backend(models.TextChoices):
        IMAP = "imap", "IMAP with an app password"
        GMAIL_API = "gmail_api", "Gmail API (Mail in the hub)"

    # Which reader the timeline sync uses. A mailbox moves to the Gmail API once its owner turns Mail on
    # (docs/EMAIL_CLIENT.md, section 4); until then it stays on IMAP.
    backend = models.CharField(max_length=12, choices=Backend.choices, default=Backend.IMAP)
    # The Gmail API's change marker: the next refresh asks Gmail what changed since this.
    history_id = models.CharField(max_length=40, blank=True, editable=False)
    # Gmail API message ids queued for the timeline sync, oldest first (crm/mail/gmail_api.py); a big back-read is
    # worked through max_messages at a time.
    pending_ids = models.JSONField(default=list, blank=True, editable=False)
    # When the inbox's first few hundred threads were indexed (crm/mail/index.py warm_inbox), so the split-inbox tabs
    # have something to show.
    inbox_indexed_at = models.DateTimeField(null=True, blank=True, editable=False)
    # When the hub finished putting CRM labels on the indexed threads once (crm/mail/crm_labels.py backfill).
    crm_labels_done_at = models.DateTimeField(null=True, blank=True, editable=False)
    # When Gmail's push watch on this mailbox runs out (a watch lasts at most 7 days; crm/mail/push.py renews it).
    watch_expires_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name_plural = "mailboxes"

    def __str__(self):
        return self.email or str(self.user)

    @property
    def password_env_var(self):
        return f"ACMI_IMAP_PASSWORD_{self.user.username.upper()}" if self.user_id else "ACMI_IMAP_PASSWORD_SHARED"

    @property
    def label(self):
        """How the hub names it: "Your mail", or a shared mailbox's name."""
        return (self.name or self.email) if self.shared else "Your mail"

    def get_password(self):
        import os

        return os.environ.get(self.password_env_var, "").replace(" ", "")


class MailThread(models.Model):
    """One conversation in a person's private mail index (docs/EMAIL_CLIENT.md, section 3): enough to draw the inbox
    quickly. Bodies and attachments are never stored; they're read from Gmail when opened. Seen only by the mailbox's
    owner (crm/mail/inbox_views.py); not in the admin, search or exports."""

    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, related_name="threads")
    gmail_id = models.CharField(max_length=40, help_text="Gmail's thread id (hex)")
    history_id = models.CharField(max_length=40, blank=True)
    subject = models.CharField(max_length=500, blank=True)
    snippet = models.TextField(blank=True)
    senders = models.JSONField(default=list, blank=True)  # [[name, address], ...] in the order they first wrote
    message_count = models.PositiveIntegerField(default=0)
    last_message_at = models.DateTimeField(null=True, blank=True)
    labels = models.JSONField(default=list, blank=True)  # Gmail label ids on any message
    in_inbox = models.BooleanField(default=False)
    unread = models.BooleanField(default=False)
    starred = models.BooleanField(default=False)
    has_attachments = models.BooleanField(default=False)
    # Which split-inbox tabs it belongs to (crm/mail/index.py): a thread can be in more than one.
    is_tenant = models.BooleanField(default=False)
    is_project = models.BooleanField(default=False)
    is_firm = models.BooleanField(default=False)
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["mailbox", "gmail_id"], name="mail_thread_unique")]
        indexes = [models.Index(fields=["mailbox", "in_inbox", "-last_message_at"])]

    def __str__(self):
        return self.subject or "(no subject)"

    @property
    def thread_id(self):
        """The decimal id the timeline stores (Activity.thread_id)."""
        try:
            return str(int(self.gmail_id, 16))
        except ValueError:
            return ""


class MailSnooze(models.Model):
    """A thread someone snoozed in the hub (crm/mail/actions.py): out of the inbox under the "ACMI/Snoozed" Gmail
    label until `until`, when the sync puts it back in the inbox, unread. Gmail's own snooze isn't open to apps."""

    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, related_name="snoozes")
    gmail_id = models.CharField(max_length=40)
    until = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["mailbox", "gmail_id"], name="mail_snooze_unique")]

    def __str__(self):
        return f"{self.gmail_id} until {self.until:%b %d %H:%M}"


class SharedMailboxToken(models.Model):
    """A shared mailbox's own Google permission (gmail.modify), from a Manager signing in as that address once. Stored
    encrypted like every Google token (crm/tokens.py); the shape gcal.access_token refreshes."""

    mailbox = models.OneToOneField(Mailbox, on_delete=models.CASCADE, related_name="shared_token")
    token = models.TextField(blank=True)
    token_secret = models.TextField(blank=True, help_text="The refresh token")
    expires_at = models.DateTimeField(null=True, blank=True)
    connected_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    connected_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Google permission for {self.mailbox}"


class MailboxAccess(models.Model):
    """A teammate's access to a shared mailbox, given by a Manager: read, or read and send (and archive, label…)."""

    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, related_name="grants")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="mailbox_grants")
    can_send = models.BooleanField(default=False)
    granted_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["mailbox", "user"], name="mailbox_access_unique")]

    def __str__(self):
        return f"{self.user} on {self.mailbox} ({'send' if self.can_send else 'read'})"


class MailboxAccessLog(models.Model):
    """Every open, send and change in a shared mailbox, and who did it (Mail in the hub › Shared mailboxes)."""

    class Action(models.TextChoices):
        OPEN = "open", "Opened"
        SEND = "send", "Sent"
        CHANGE = "change", "Changed"
        FILE = "file", "Filed"
        DOWNLOAD = "download", "Downloaded a file"

    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, related_name="access_log")
    user = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="+")
    action = models.CharField(max_length=10, choices=Action.choices)
    gmail_id = models.CharField(max_length=40, blank=True)
    detail = models.CharField(max_length=300, blank=True)
    at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-at"]


class MailFiling(models.Model):
    """A thread filed to a record by hand (crm/mail/filing.py), from the hub's File to… or by putting a CRM label on it
    in Gmail. Keeps what the filing added to timelines, so taking it off removes exactly that."""

    class Source(models.TextChoices):
        HUB = "hub", "File to… in the hub"
        GMAIL = "gmail", "A CRM label added in Gmail"

    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, related_name="filings")
    gmail_id = models.CharField(max_length=40)
    thread_id = models.CharField(max_length=100, db_index=True, help_text="Decimal, as Activity.thread_id")
    subject = models.CharField(max_length=500, blank=True)
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.CASCADE, related_name="mail_filings")
    opportunity = models.ForeignKey(Opportunity, null=True, blank=True, on_delete=models.CASCADE,
                                    related_name="mail_filings")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.CASCADE, related_name="mail_filings")
    firm = models.ForeignKey("Firm", null=True, blank=True, on_delete=models.CASCADE, related_name="mail_filings")
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.HUB)
    filed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    added_activities = models.JSONField(default=list, blank=True)  # Activity ids this filing created
    added_firm_emails = models.JSONField(default=list, blank=True)

    class Meta:
        indexes = [models.Index(fields=["mailbox", "gmail_id"])]

    @property
    def target(self):
        return self.opportunity or self.company or self.project or self.firm

    def __str__(self):
        return f"{self.subject or self.gmail_id} → {self.target}"


class MailCrmLabel(models.Model):
    """A Gmail label the hub keeps for a record ("ACMI/Projects/MEIH"), in one person's mailbox (crm/mail/crm_labels.py)."""

    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, related_name="crm_labels")
    gmail_label_id = models.CharField(max_length=100)
    name = models.CharField(max_length=225)
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    firm = models.ForeignKey("Firm", null=True, blank=True, on_delete=models.CASCADE, related_name="+")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["mailbox", "gmail_label_id"], name="mail_crm_label_unique")]

    def __str__(self):
        return self.name


class MailCrmLabelOnThread(models.Model):
    """The hub put this CRM label on this thread. removed=True: the person took it off in Gmail, so it stays off."""

    label = models.ForeignKey(MailCrmLabel, on_delete=models.CASCADE, related_name="threads")
    gmail_id = models.CharField(max_length=40, db_index=True)
    removed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["label", "gmail_id"], name="mail_crm_label_thread_unique")]


class MailComment(models.Model):
    """A team note on an email thread (crm/mail/comments.py). Seen only by its author and the people it @mentions,
    in whichever mailbox they have the thread: it's matched by the emails' Message-IDs, not one mailbox's ids."""

    author = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="mail_comments")
    body = models.TextField()
    subject = models.CharField(max_length=500, blank=True)
    mentions = models.ManyToManyField(User, blank=True, related_name="mail_comment_mentions")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.author}: {self.body[:40]}"


class MailCommentKey(models.Model):
    comment = models.ForeignKey(MailComment, on_delete=models.CASCADE, related_name="keys")
    message_id = models.CharField(max_length=500, db_index=True)


class MailLabel(models.Model):
    """A Gmail label in someone's mailbox (their folders), kept to draw the folder pane without asking Gmail."""

    mailbox = models.ForeignKey(Mailbox, on_delete=models.CASCADE, related_name="labels")
    gmail_id = models.CharField(max_length=100)
    name = models.CharField(max_length=225)
    system = models.BooleanField(default=False)
    text_color = models.CharField(max_length=7, blank=True)
    bg_color = models.CharField(max_length=7, blank=True)
    unread = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["mailbox", "gmail_id"], name="mail_label_unique")]
        ordering = ["name"]

    def __str__(self):
        return self.name


class MailConnection(models.Model):
    """One person's Mail in the hub switch (docs/EMAIL_CLIENT.md). Off until they turn it on; crm/mail/connect.py.

    The Gmail permission itself lives in their (encrypted) Google token; this row says whether they want it used.
    """

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="mail_connection")
    enabled = models.BooleanField(default=False)
    google_email = models.EmailField(blank=True)
    turned_on_at = models.DateTimeField(null=True, blank=True)
    turned_off_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    def __str__(self):
        return f"Mail for {self.user} ({'on' if self.enabled else 'off'})"


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


class InviteCancel(models.Model):
    """An emailed invite that was cancelled (one occurrence, or the whole meeting): a copy of the original invite
    forwarded later doesn't put the meeting back."""

    external_id = models.CharField(max_length=255, unique=True, help_text="invite:<UID>[:<RECURRENCE-ID>]")
    version = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.external_id


class FilingSnooze(models.Model):
    """"Not now" on a Needs filing card: the domain's card stays out of the inbox (and its count) until the date."""

    domain = models.CharField(max_length=255, unique=True)
    until = models.DateField()
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.domain} until {self.until}"


class AddressSuggestion(models.Model):
    """A free-mail sender (gmail.com and the like) whose display name matches a Person: "Same person, add this
    address". The email waits here until someone says yes (the address goes on the person and the email on their
    timeline) or no."""

    class Status(models.TextChoices):
        OPEN = "open", "Waiting"
        ADDED = "added", "Address added"
        DISMISSED = "dismissed", "Not them"

    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="address_suggestions")
    address = models.EmailField(max_length=255, db_index=True)
    name = models.CharField(max_length=255, blank=True)
    mailbox = models.ForeignKey(Mailbox, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    message_id = models.CharField(max_length=500)
    thread_id = models.CharField(max_length=100, blank=True)
    occurred_at = models.DateTimeField()
    kind = models.CharField(max_length=20, choices=Activity.Kind.choices)
    outcome = models.CharField(max_length=60, blank=True)
    subject = models.CharField(max_length=500, blank=True)
    snippet = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True)
    decided_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-occurred_at"]
        constraints = [
            models.UniqueConstraint(fields=["message_id", "address"], name="address_suggestion_once_per_message"),
        ]

    def __str__(self):
        return f"{self.address} → {self.person}"


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
    # Owner group (crm/land/portfolio.py): stored parcels whose owners share a name in the county, or a mailing
    # address anywhere. Blank = no other stored parcel in the group.
    portfolio_key = models.CharField(max_length=24, blank=True, db_index=True)
    portfolio_parcels = models.PositiveIntegerField(null=True, blank=True)
    portfolio_acres = models.FloatField(null=True, blank=True)
    # Last transfer, where the county publishes it (crm/land/sales.py). Price stays empty where it isn't published
    # (Texas never publishes it).
    last_sale_date = models.DateField(null=True, blank=True)
    last_sale_price = models.BigIntegerField(null=True, blank=True)
    last_sale_type = models.CharField(max_length=80, blank=True, help_text="How it was conveyed, as the county says")
    last_sale_arms_length = models.BooleanField(null=True, blank=True, help_text="Blank = the county doesn't say")
    last_sale_deed = models.CharField(max_length=60, blank=True, help_text="Deed book / page or document number")
    last_sale_source = models.CharField(max_length=120, blank=True)
    # Off-market signals (crm/land/signals.py): codes with a space either side, e.g. " long_hold estate ".
    signals = models.CharField(max_length=120, blank=True)
    motivation = models.PositiveSmallIntegerField(default=0, db_index=True,
                                                  help_text="Weighted count of off-market signals (not the buy-box score)")
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
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="owned_sites",
                              help_text="Who on the team runs this site")
    team = models.ManyToManyField(User, blank=True, related_name="site_teams", help_text="Everyone working the site")
    parcels = models.ManyToManyField(Parcel, blank=True, related_name="sites")
    notes = models.TextField(blank=True)
    qd_design = models.JSONField(default=dict, blank=True, help_text="QD site layout on the assembled boundary")
    milestones = models.JSONField(default=dict, blank=True, help_text="{milestone key: 'YYYY-MM-DD'} (see MILESTONES)")
    site_plan = models.JSONField(default=dict, blank=True, help_text="Quick site plan inputs (zone, overrides, program)")
    listing = models.JSONField(default=dict, blank=True, help_text="Facts from the broker flyer or listing (see crm/land/briefs.py LISTING_KEYS)")
    ai_summary = models.JSONField(default=dict, blank=True, help_text="Claude's summary for the site card (crm/land/site_card.py)")
    brief_options = models.JSONField(default=dict, blank=True, help_text='Brief layout: {"hide": [section keys], "extras": [{"title", "body"}]} (see crm/land/brief_options.py)')
    flyer = models.FileField("broker flyer", upload_to="flyers/%Y/%m/", blank=True)
    flyer_read_at = models.DateTimeField(null=True, blank=True, help_text="When Claude last read the flyer")
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["campus__code", "project__name", "name"]

    def __str__(self):
        return self.name

    # Furthest along first; dropped last.
    STATUS_ORDER = Order("LandSite.Status", ["acquired", "contract", "loi", "outreach", "review", "prospect", "dropped"])

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
        return reverse("crm:land_site_detail", args=[self.pk])

    @property
    def map_url(self):
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


class SaleComp(models.Model):
    """A land sale used as a comparable: from county records where they publish a price and the sale looks
    arms-length (crm/land/sales.py sync_comps), or entered by the team (Texas publishes no prices)."""

    class Source(models.TextChoices):
        COUNTY = "county", "County record"
        TEAM = "team", "Entered by the team"

    source = models.CharField(max_length=10, choices=Source.choices, default=Source.TEAM, db_index=True)
    record_key = models.CharField(max_length=120, blank=True, help_text="County comps: parcel key(s) and sale date")
    county = models.ForeignKey(LandCounty, null=True, blank=True, on_delete=models.SET_NULL, related_name="sale_comps")
    parcel = models.ForeignKey(Parcel, null=True, blank=True, on_delete=models.SET_NULL, related_name="sale_comps")
    site = models.ForeignKey(LandSite, null=True, blank=True, on_delete=models.SET_NULL, related_name="sale_comps")
    sale_date = models.DateField(db_index=True)
    price = models.BigIntegerField()
    acres = models.FloatField()
    price_per_acre = models.FloatField(editable=False, db_index=True)
    place = models.CharField(max_length=200, blank=True, help_text="Address or description of the land")
    buyer = models.CharField(max_length=255, blank=True)
    seller = models.CharField(max_length=255, blank=True)
    arms_length = models.BooleanField(null=True, blank=True, help_text="Blank = not known")
    conveyance = models.CharField(max_length=80, blank=True)
    deed = models.CharField(max_length=60, blank=True)
    improved = models.BooleanField(default=False, help_text="Buildings on it: the price isn't land alone")
    parcels_count = models.PositiveSmallIntegerField(default=1, help_text="Parcels the one price covered")
    lat = models.FloatField(null=True, blank=True, db_index=True)
    lon = models.FloatField(null=True, blank=True)
    source_note = models.CharField(max_length=255, blank=True, help_text="Where the figure came from (broker, deed...)")
    notes = models.TextField(blank=True, help_text="Internal: never printed on a brief")
    internal_only = models.BooleanField(default=False, help_text="Keep off outside reports (e.g. broker intel under NDA)")
    flyer = models.FileField("broker flyer", upload_to="comps/%Y/%m/", blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-sale_date"]
        constraints = [models.UniqueConstraint(fields=["county", "record_key"], condition=models.Q(source="county"),
                                               name="salecomp_county_record_unique")]

    def __str__(self):
        return f"{self.sale_date:%Y-%m-%d} · {self.acres:,.0f} ac · ${self.price:,}"

    def save(self, *args, **kwargs):
        self.price_per_acre = round(self.price / self.acres, 2) if self.acres else 0
        super().save(*args, **kwargs)


class Assignment(models.Model):
    """A land search for a client: many candidate sites, each with a diligence report, ranked against each other.

    Clients see it through a private share link (crm/land/assignments.py, the /g/<token>/ pages).
    """

    name = models.CharField(max_length=200, help_text="e.g. Elbit 26 Defense land search")
    company = models.ForeignKey("Company", null=True, blank=True, on_delete=models.SET_NULL, related_name="land_searches",
                                help_text="The client's company record: the search shows on its Site searches tab")
    client = models.CharField(max_length=200, blank=True, help_text="Name shown to the client; defaults to the company's")
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


# ---------------------------------------------------------------------------
# Contracts (crm/contracts/): the agreement file, its critical dates, and Claude's cited answers
# ---------------------------------------------------------------------------


class Contract(models.Model):
    """One agreement and everything signed or exchanged under it, modeled on Notessa: the original, its amendments,
    restatements, side letters and negotiation drafts in order (ContractDocument), the dates nobody can miss
    (ContractDate), and Claude's answers and abstracts, each pointing back to the exact language (ContractAnswer)."""

    class Kind(models.TextChoices):
        LOI = "loi", "Letter of intent"
        PSA = "psa", "Purchase and sale agreement"
        OPTION = "option", "Option to purchase"
        LEASE = "lease", "Lease"
        PDA = "pda", "Development agreement (PDA / DMA)"
        JV = "jv", "JV / operating agreement"
        LOAN = "loan", "Loan agreement"
        EASEMENT = "easement", "Easement / REA"
        SERVICES = "services", "Design / construction contract"
        NDA = "nda", "NDA"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        DRAFTING = "drafting", "Drafting"
        NEGOTIATING = "negotiating", "Negotiating"
        SIGNED = "signed", "Signed"
        CLOSED = "closed", "Closed / complete"
        TERMINATED = "terminated", "Terminated"

    class Decision(models.TextChoices):
        NONE = "", "Not decided"
        PROCEED = "proceed", "Proceed (deposit goes hard)"
        EXTEND = "extend", "Extend feasibility"
        TERMINATE = "terminate", "Terminate"

    OPEN_STATUSES = Group("Contract.Status", ["drafting", "negotiating", "signed"])

    title = models.CharField(max_length=200, help_text="e.g. Wamsley PSA")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.PSA)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.NEGOTIATING, db_index=True)
    counterparty = models.CharField(max_length=200, blank=True, help_text="Seller, landlord, tenant, county, lender…")
    our_entity = models.CharField("our entity", max_length=200, blank=True, help_text="The ACMI entity that signs")
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.SET_NULL, related_name="contracts")
    opportunity = models.ForeignKey(Opportunity, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="contracts", verbose_name="tenant deal")
    campus = models.ForeignKey(Campus, null=True, blank=True, on_delete=models.SET_NULL, related_name="contracts")
    site = models.ForeignKey(LandSite, null=True, blank=True, on_delete=models.SET_NULL, related_name="contracts")
    parcel_deals = models.ManyToManyField(
        SiteParcelDeal, blank=True, related_name="contracts",
        help_text="The owners' parcels it covers, when it is one owner's deal inside an assemblage",
    )
    price = models.DecimalField("price / value ($)", max_digits=14, decimal_places=2, null=True, blank=True)
    acres = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    effective_date = models.DateField(null=True, blank=True, help_text="When it was signed or took effect")
    waiting_on = models.CharField("ball in court", max_length=6, choices=CampusUpdate.Waiting.choices, blank=True)
    waiting_since = models.DateField(null=True, blank=True)
    next_step = models.CharField(max_length=300, blank=True)
    counsel = models.CharField(max_length=200, blank=True, help_text="Our lawyer on it")
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="contracts",
                              help_text="Who on the team runs it; gets the deadline reminders")
    notes = models.TextField(blank=True)
    decision = models.CharField("feasibility decision", max_length=10, choices=Decision.choices, blank=True,
                                help_text="The go / no-go at the end of due diligence")
    decision_on = models.DateField(null=True, blank=True)
    decision_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decision_note = models.TextField(blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("crm:contract_detail", args=[self.pk])

    @property
    def is_open(self):
        return self.status in self.OPEN_STATUSES

    @property
    def home_label(self):
        return " / ".join(str(x) for x in [self.campus, self.site] if x)


class ContractDocument(models.Model):
    """One document in a contract's file. Signed documents form the chain the terms come from: the original, then
    amendments in date order, where an amended and restated agreement replaces everything before it. Drafts are the
    negotiation rounds. Text is pulled out on upload (crm/contracts/text.py) for the reader, compare and Claude."""

    class Kind(models.TextChoices):
        ORIGINAL = "original", "Original agreement"
        AMENDMENT = "amendment", "Amendment"
        RESTATED = "restated", "Amended and restated agreement"
        SIDE_LETTER = "side_letter", "Side letter"
        ASSIGNMENT = "assignment", "Assignment / assumption"
        NOTICE = "notice", "Notice / extension / waiver"
        EXHIBIT = "exhibit", "Exhibit / schedule"
        ESTOPPEL = "estoppel", "Estoppel / SNDA"
        DRAFT = "draft", "Draft (negotiation round)"
        OTHER = "other", "Other"

    class Sender(models.TextChoices):
        NONE = "", "–"
        US = "us", "Us"
        THEM = "them", "Them"

    class TextStatus(models.TextChoices):
        NONE = "", "Not read"
        OK = "ok", "Text read"
        SCAN = "scan", "Scanned (no text layer)"
        UNSUPPORTED = "unsupported", "File type not readable"
        ERROR = "error", "Could not read"

    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="documents")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.ORIGINAL)
    title = models.CharField(max_length=300)
    file = models.FileField(upload_to="contracts/%Y/%m/", blank=True)
    url = models.URLField("link", max_length=1000, blank=True, help_text="Where the signed copy lives (Drive)")
    dated = models.DateField("dated", null=True, blank=True,
                             help_text="Effective date of a signed document; the day a draft was sent")
    sent_by = models.CharField(max_length=4, choices=Sender.choices, blank=True, help_text="Drafts: who sent it")
    summary = models.TextField(blank=True, help_text="What it does, or what changed in this round")
    text = models.TextField(blank=True, editable=False)
    page_starts = models.JSONField(default=list, blank=True, editable=False,
                                   help_text="Character offset where each page starts in `text`")
    pages = models.PositiveIntegerField(default=0)
    text_status = models.CharField(max_length=12, choices=TextStatus.choices, blank=True)
    position = models.PositiveSmallIntegerField(default=0, help_text="Order among documents with the same date")
    author = models.CharField("redlined by", max_length=200, blank=True,
                              help_text="Who made this round's changes, e.g. Venable (seller's counsel)")
    tracked = models.JSONField(default=list, blank=True, editable=False,
                               help_text='Word tracked changes: [{"type": "ins"|"del", "start", "end", "author", '
                                         '"date", "text"}]')
    timeline = models.JSONField(default=list, blank=True, editable=False,
                                help_text="The dates this version sets, as rules (crm/contracts/timeline.py)")
    timeline_at = models.DateTimeField(null=True, blank=True, editable=False)
    added_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = [models.F("dated").asc(nulls_last=True), "position", "id"]

    def __str__(self):
        return self.title

    @property
    def is_draft(self):
        return self.kind == self.Kind.DRAFT

    @property
    def href(self):
        return reverse("crm:contract_file", args=[self.pk]) if self.file else self.url

    def page_at(self, index):
        """1-based page holding character `index` of the text."""
        from bisect import bisect_right

        return max(1, bisect_right(self.page_starts or [0], index))

    @property
    def made_by(self):
        """Who made this version's changes: the named author, else our uploader for our drafts, else the other side."""
        if self.author:
            return self.author
        if self.sent_by == self.Sender.THEM:
            return self.contract.counterparty or "The other side"
        if self.added_by_id:
            return self.added_by.get_full_name() or self.added_by.username
        return "ACMI" if self.sent_by == self.Sender.US else ""


class ContractTemplate(models.Model):
    """A reusable review method, as in Notessa: the points an abstract covers, in order, and how to write them up.
    Built-in ones (crm/contracts/methods.py) follow the code; the team can copy one and make it its own."""

    key = models.CharField(max_length=40, blank=True, db_index=True, help_text="Built-in template key; blank = team-made")
    name = models.CharField(max_length=120)
    description = models.CharField(max_length=300, blank=True)
    kinds = models.JSONField(default=list, blank=True, help_text="Contract kinds it suits; empty = any")
    points = models.JSONField(default=list, blank=True, help_text='[{"label": ..., "ask": ...}] in order')
    instructions = models.TextField(blank=True, help_text="How to write it up (tone, length, what to flag)")
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["key"], condition=~models.Q(key=""),
                                               name="contract_template_key_unique")]

    def __str__(self):
        return self.name

    @property
    def builtin(self):
        return bool(self.key)


class ContractAnswer(models.Model):
    """Claude's cited answer to a question about a contract, or an abstract from a template. `blocks` keeps the answer
    as Claude wrote it, with the passages each part rests on:
    [{"text": ..., "cites": [{"doc": id, "title": ..., "start": .., "end": .., "page": .., "quote": ..}]}]."""

    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="answers")
    template = models.ForeignKey(ContractTemplate, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="answers")
    question = models.TextField(help_text="The question asked, or the template's name")
    blocks = models.JSONField(default=list, blank=True)
    documents = models.JSONField(default=list, blank=True, help_text='[{"id": .., "title": ..}] what Claude read')
    model = models.CharField(max_length=60, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cache_read_tokens = models.PositiveIntegerField(default=0)
    pinned = models.BooleanField(default=False, help_text="Keep it at the top of the contract")
    asked_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-pinned", "-created_at"]

    def __str__(self):
        return self.question[:60]

    @property
    def is_abstract(self):
        return self.template_id is not None or self.question.startswith("Abstract: ")  # template since deleted

    @property
    def text(self):
        return "".join(b.get("text", "") for b in self.blocks or [])


class ContractDate(models.Model):
    """A date the team can't miss: a deposit due or going hard, the end of feasibility, title objections, closing, an
    extension or renewal notice. The contract's owner is reminded 30, 14, 7 and 1 days ahead (crm/notify.py)."""

    class Kind(models.TextChoices):
        DEPOSIT = "deposit", "Deposit due"
        HARD = "hard", "Deposit goes hard"
        FEASIBILITY = "feasibility", "Feasibility / due diligence ends"
        TITLE = "title", "Title / survey objections"
        APPROVAL = "approval", "Approvals / entitlements"
        NOTICE = "notice", "Notice deadline"
        EXTENSION = "extension", "Extension / renewal option"
        CLOSING = "closing", "Closing"
        EXPIRATION = "expiration", "Expiration"
        PAYMENT = "payment", "Payment / rent"
        COVENANT = "covenant", "Covenant / reporting"
        OTHER = "other", "Other"

    class Unit(models.TextChoices):
        DAYS = "days", "days"
        BUSINESS = "business", "business days"
        WEEKS = "weeks", "weeks"
        MONTHS = "months", "months"

    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="dates")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.OTHER)
    label = models.CharField(max_length=200)
    date = models.DateField(null=True, blank=True, db_index=True,
                            help_text="Empty while it hangs on an event (e.g. 30 days after approvals)")
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    rule = models.CharField("how it's set", max_length=300, blank=True, help_text="e.g. 30 days after the Effective Date")
    confirmed = models.BooleanField(default=True, help_text="Dates Claude found wait here until someone keeps them")
    done = models.BooleanField(default=False)
    done_at = models.DateTimeField(null=True, blank=True)
    done_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    source = models.ForeignKey(ContractDocument, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    source_start = models.PositiveIntegerField(null=True, blank=True)
    source_end = models.PositiveIntegerField(null=True, blank=True)
    quote = models.TextField(blank=True, help_text="The language that sets the date")
    from_claude = models.BooleanField(default=False)
    critical = models.BooleanField("critical date", default=True, help_text="Shows on the dashboard's calendar")
    key = models.SlugField(max_length=40, blank=True, help_text="Stable name other dates count from, e.g. feasibility_end")
    anchor = models.SlugField(max_length=40, blank=True,
                              help_text="What it counts from: effective, or another date's key; empty = a fixed date")
    offset = models.IntegerField(null=True, blank=True, help_text="How many units after the anchor")
    unit = models.CharField(max_length=10, choices=Unit.choices, blank=True)
    extends = models.SlugField(max_length=40, blank=True,
                               help_text="An extension option: the key of the date it pushes out")
    extend_by = models.IntegerField(null=True, blank=True)
    extend_unit = models.CharField(max_length=10, choices=Unit.choices, blank=True)
    history = models.JSONField(default=list, blank=True, editable=False,
                               help_text='Moves: [{"from", "to", "why", "by", "on"}]')
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["done", models.F("date").asc(nulls_last=True), "id"]

    def __str__(self):
        return f"{self.label} ({self.date or 'date not fixed'})"

    def days_left(self, today=None):
        return (self.date - (today or timezone.localdate())).days if self.date else None


class DiligenceItem(models.Model):
    """One due-diligence item on a site ACMI is buying (title commitment, ALTA survey, Phase I, geotech, zoning letter,
    will-serves…): who is doing it, what it costs, when it was ordered, is due and came in, and what it found. A site
    starts from the standard checklist the client reports use (crm/land/diligence.py). The report itself can be
    uploaded; Claude summarizes it with citations to its pages."""

    class Status(models.TextChoices):
        TODO = "todo", "Not started"
        ORDERED = "ordered", "Ordered"
        RECEIVED = "received", "Received, to review"
        CLEAR = "clear", "Reviewed: clear"
        ISSUE = "issue", "Reviewed: issue"
        WAIVED = "waived", "Waived / N/A"

    OPEN = ("todo", "ordered", "received")

    site = models.ForeignKey(LandSite, on_delete=models.CASCADE, related_name="diligence")
    contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.SET_NULL, related_name="diligence",
                                 help_text="The agreement whose feasibility period it has to finish in")
    ref = models.CharField(max_length=8, blank=True, help_text="Checklist reference, e.g. D-1")
    block = models.CharField(max_length=60, default="Other")
    name = models.CharField(max_length=200)
    how = models.CharField("deliverable / how verified", max_length=200, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.TODO, db_index=True)
    consultant = models.CharField(max_length=200, blank=True, help_text="Firm doing it (title company, surveyor…)")
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                              help_text="Who on the team chases it; gets the reminders")
    budget = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    cost = models.DecimalField("invoiced", max_digits=12, decimal_places=2, null=True, blank=True)
    ordered_on = models.DateField(null=True, blank=True)
    due_on = models.DateField(null=True, blank=True)
    received_on = models.DateField(null=True, blank=True)
    findings = models.TextField(blank=True)
    file = models.FileField("report", upload_to="diligence/%Y/%m/", blank=True)
    url = models.URLField("link", max_length=1000, blank=True)
    text = models.TextField(blank=True, editable=False)
    page_starts = models.JSONField(default=list, blank=True, editable=False)
    pages = models.PositiveIntegerField(default=0)
    text_status = models.CharField(max_length=12, blank=True)
    summary = models.JSONField(default=list, blank=True, help_text="Claude's cited summary of the report")
    summary_model = models.CharField(max_length=60, blank=True)
    summary_at = models.DateTimeField(null=True, blank=True)
    position = models.PositiveSmallIntegerField(default=0)
    updated_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.site} · {self.name}"

    @property
    def is_open(self):
        return self.status in self.OPEN

    @property
    def title(self):  # what citations call the report
        return self.name

    @property
    def href(self):
        return reverse("crm:land_diligence_file", args=[self.pk]) if self.file else self.url

    def page_at(self, index):
        from bisect import bisect_right

        return max(1, bisect_right(self.page_starts or [0], index))


class ClosingItem(models.Model):
    """One thing that has to happen to close a purchase, or right after it: each side's deliverables, the money,
    recording, and the clean-up (title policy, tax accounts, insurance). Defaults in crm/contracts/closing.py."""

    class Side(models.TextChoices):
        BUYER = "buyer", "Us (buyer)"
        SELLER = "seller", "Seller"
        TITLE = "title", "Title / escrow"
        LENDER = "lender", "Lender"
        POST = "post", "After closing"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        DONE = "done", "Done"
        NA = "na", "Not needed"

    contract = models.ForeignKey(Contract, on_delete=models.CASCADE, related_name="closing_items")
    side = models.CharField(max_length=8, choices=Side.choices, default=Side.BUYER)
    name = models.CharField(max_length=200)
    responsible = models.CharField(max_length=120, blank=True, help_text="Who delivers it (person or firm)")
    due_on = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=6, choices=Status.choices, default=Status.OPEN)
    done_on = models.DateField(null=True, blank=True)
    done_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    notes = models.CharField(max_length=300, blank=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Entitlements (crm/entitlements/): every approval a project or site needs, its steps, conditions and papers
# ---------------------------------------------------------------------------


class Approval(models.Model):
    """One entitlement or permit a project or site needs (rezoning, special use, variance, site plan, plat, grading,
    stormwater, wetlands, the DDESB explosives safety site plan, road access, building permit): the agency and case,
    where it stands, who has the ball, its dated steps (ApprovalStep), the conditions it came with
    (ApprovalCondition) and its papers (ApprovalDocument)."""

    class Kind(models.TextChoices):
        REZONING = "rezoning", "Rezoning"
        SPECIAL_USE = "special_use", "Special / conditional use permit"
        VARIANCE = "variance", "Variance"
        SITE_PLAN = "site_plan", "Site plan"
        SUBDIVISION = "subdivision", "Subdivision / plat"
        GRADING = "grading", "Grading / erosion and sediment control"
        STORMWATER = "stormwater", "Stormwater"
        WETLANDS = "wetlands", "Wetlands / waters (404 / 401)"
        FLOODPLAIN = "floodplain", "Floodplain"
        ESSP = "essp", "DDESB explosives safety site plan"
        ACCESS = "access", "Road access / entrance permit"
        UTILITY = "utility", "Utility extension / agreement"
        ENVIRONMENTAL = "environmental", "Environmental review (NEPA / state)"
        HISTORIC = "historic", "Historic / cultural (Section 106)"
        AIR_WATER = "air_water", "Air or water discharge permit"
        BUILDING = "building", "Building permit"
        OCCUPANCY = "occupancy", "Certificate of occupancy"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        PLANNED = "planned", "Not filed"
        PREAPP = "preapp", "Pre-application"
        SUBMITTED = "submitted", "Submitted"
        REVIEW = "review", "In review / comments"
        HEARING = "hearing", "Hearing scheduled"
        APPROVED = "approved", "Approved"
        DENIED = "denied", "Denied"
        WITHDRAWN = "withdrawn", "Withdrawn"
        EXPIRED = "expired", "Expired"

    OPEN = ("planned", "preapp", "submitted", "review", "hearing")

    title = models.CharField(max_length=200, help_text="e.g. Sign height variance")
    kind = models.CharField(max_length=14, choices=Kind.choices, default=Kind.OTHER)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PLANNED, db_index=True)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="approvals")
    site = models.ForeignKey(LandSite, null=True, blank=True, on_delete=models.SET_NULL, related_name="approvals",
                             verbose_name="land site")
    campus = models.ForeignKey(Campus, null=True, blank=True, on_delete=models.SET_NULL, related_name="approvals")
    contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.SET_NULL, related_name="approvals",
                                 help_text="The purchase agreement whose approvals contingency it serves")
    jurisdiction = models.CharField("agency / board", max_length=200, blank=True,
                                    help_text="e.g. Charles County Board of Appeals, USACE Baltimore District")
    case_number = models.CharField(max_length=60, blank=True)
    consultant = models.CharField("engineer / attorney", max_length=200, blank=True)
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="approvals",
                              help_text="Who on the team runs it; gets the reminders")
    waiting_on = models.CharField("ball in court", max_length=6, choices=CampusUpdate.Waiting.choices, blank=True)
    waiting_since = models.DateField(null=True, blank=True)
    next_step = models.CharField(max_length=300, blank=True)
    needed_by = models.DateField(null=True, blank=True, help_text="When the deal needs it (closing, permits, start of work)")
    submitted_on = models.DateField(null=True, blank=True)
    approved_on = models.DateField("decision date", null=True, blank=True)
    expires_on = models.DateField(null=True, blank=True, help_text="When the approval lapses if not used or extended")
    appeal_ends_on = models.DateField(null=True, blank=True)
    budget = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    cost = models.DecimalField("spent", max_digits=12, decimal_places=2, null=True, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("crm:approval_detail", args=[self.pk])

    @property
    def is_open(self):
        return self.status in self.OPEN

    @property
    def home_label(self):
        return " / ".join(str(x) for x in [self.campus, self.project or self.site] if x)


class ApprovalStep(models.Model):
    """A dated step in an approval: the pre-application meeting, the submittal, comments, a resubmittal, a hearing, the
    decision, the end of the appeal period. Steps not yet done are what the dashboard and reminders show."""

    class Kind(models.TextChoices):
        PREAPP = "preapp", "Pre-application meeting"
        SUBMITTED = "submitted", "Submitted"
        COMMENTS = "comments", "Comments received"
        RESUBMITTED = "resubmitted", "Resubmitted"
        HEARING = "hearing", "Hearing / meeting"
        DECISION = "decision", "Decision"
        APPEAL = "appeal", "Appeal period ends"
        OTHER = "other", "Other"

    approval = models.ForeignKey(Approval, on_delete=models.CASCADE, related_name="steps")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.OTHER)
    date = models.DateField()
    body = models.CharField("board / meeting", max_length=200, blank=True, help_text="e.g. Planning Commission")
    note = models.TextField(blank=True)
    done = models.BooleanField(default=False, help_text="It happened")
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["date", "id"]

    def __str__(self):
        return f"{self.get_kind_display()} {self.date}"

    @property
    def label(self):
        return f"{self.get_kind_display()}" + (f" ({self.body})" if self.body else "")


class ApprovalDocument(models.Model):
    """A paper in an approval's file: the application, a staff report, a comment letter, the resolution or approval
    letter, plans. Text is read on upload so Claude can summarize it and pull out the conditions, with citations."""

    class Kind(models.TextChoices):
        APPLICATION = "application", "Application"
        STAFF_REPORT = "staff_report", "Staff report"
        COMMENTS = "comments", "Comment letter"
        DECISION = "decision", "Resolution / approval letter"
        PLANS = "plans", "Plans"
        OTHER = "other", "Other"

    approval = models.ForeignKey(Approval, on_delete=models.CASCADE, related_name="documents")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.OTHER)
    title = models.CharField(max_length=300)
    file = models.FileField(upload_to="entitlements/%Y/%m/", blank=True)
    url = models.URLField("link", max_length=1000, blank=True)
    dated = models.DateField(null=True, blank=True)
    text = models.TextField(blank=True, editable=False)
    page_starts = models.JSONField(default=list, blank=True, editable=False)
    pages = models.PositiveIntegerField(default=0)
    text_status = models.CharField(max_length=12, blank=True)
    summary = models.JSONField(default=list, blank=True, help_text="Claude's cited summary")
    summary_model = models.CharField(max_length=60, blank=True)
    summary_at = models.DateTimeField(null=True, blank=True)
    added_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = [models.F("dated").asc(nulls_last=True), "id"]

    def __str__(self):
        return self.title

    @property
    def href(self):
        return reverse("crm:approval_file", args=[self.pk]) if self.file else self.url

    def page_at(self, index):
        from bisect import bisect_right

        return max(1, bisect_right(self.page_starts or [0], index))


class ApprovalCondition(models.Model):
    """A condition an approval came with: what has to be done, before what (plat, building permit, CO) or by when, and
    whether it's met. Conditions Claude finds in a resolution wait here until someone keeps them."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        MET = "met", "Met"
        NA = "na", "Not applicable"

    approval = models.ForeignKey(Approval, on_delete=models.CASCADE, related_name="conditions")
    text = models.TextField()
    timing = models.CharField("when", max_length=200, blank=True, help_text="e.g. Before plat recordation; ongoing")
    due_on = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=4, choices=Status.choices, default=Status.OPEN)
    confirmed = models.BooleanField(default=True)
    source = models.ForeignKey(ApprovalDocument, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    source_start = models.PositiveIntegerField(null=True, blank=True)
    source_end = models.PositiveIntegerField(null=True, blank=True)
    quote = models.TextField(blank=True)
    from_claude = models.BooleanField(default=False)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.text[:60]


# ---------------------------------------------------------------------------
# Build records (crm/build/): a project's schedule, team, GC selection, budget, change orders and draws. Summaries
# of what the GC's own system holds in detail.
# ---------------------------------------------------------------------------


class BuildMilestone(models.Model):
    """A schedule milestone (design, procurement, construction, closeout) with the baseline date it was planned for,
    today's forecast and the actual date. Slip = forecast (or actual) against the baseline."""

    class Phase(models.TextChoices):
        DESIGN = "design", "Design"
        PROCUREMENT = "procurement", "Procurement"
        CONSTRUCTION = "construction", "Construction"
        CLOSEOUT = "closeout", "Closeout"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="milestones")
    phase = models.CharField(max_length=12, choices=Phase.choices, default=Phase.CONSTRUCTION)
    name = models.CharField(max_length=120)
    baseline = models.DateField(null=True, blank=True, help_text="The date in the approved schedule")
    forecast = models.DateField(null=True, blank=True)
    actual = models.DateField(null=True, blank=True)
    note = models.CharField(max_length=300, blank=True)
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.name

    @property
    def slip(self):
        """Days late (positive) or early against the baseline; None without both dates."""
        when = self.actual or self.forecast
        return (when - self.baseline).days if when and self.baseline else None


class TeamMember(models.Model):
    """A firm on a project's team: architect, civil, structural, MEP, geotech, surveyor, GC, owner's rep, attorney,
    the explosives safety (QD) consultant. Who, the contract amount and what's been invoiced."""

    class Role(models.TextChoices):
        ARCHITECT = "architect", "Architect"
        CIVIL = "civil", "Civil engineer"
        STRUCTURAL = "structural", "Structural engineer"
        MEP = "mep", "MEP engineer"
        GEOTECH = "geotech", "Geotechnical engineer"
        SURVEYOR = "surveyor", "Surveyor"
        ENVIRONMENTAL = "environmental", "Environmental consultant"
        QD = "qd", "Explosives safety / QD consultant"
        GC = "gc", "General contractor"
        OWNERS_REP = "owners_rep", "Owner's representative"
        ATTORNEY = "attorney", "Attorney"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        PROPOSED = "proposed", "Proposed"
        ENGAGED = "engaged", "Engaged"
        COMPLETE = "complete", "Complete"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="team")
    role = models.CharField(max_length=14, choices=Role.choices, default=Role.OTHER)
    firm = models.CharField(max_length=200)
    contact = models.CharField(max_length=200, blank=True, help_text="Name, email or phone")
    fee = models.DecimalField("contract ($)", max_digits=14, decimal_places=2, null=True, blank=True)
    invoiced = models.DecimalField("invoiced ($)", max_digits=14, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ENGAGED)
    notes = models.CharField(max_length=300, blank=True)
    # The firm's entry in the directory (found or started by name on save), never a Company, so it stays out of
    # outreach and its page shows every project it's on.
    directory = models.ForeignKey("Firm", null=True, blank=True, on_delete=models.SET_NULL, related_name="team_roles")

    # The directory type a firm first named on a team starts as.
    FIRM_KINDS = {"architect": "architect", "civil": "engineer", "structural": "engineer", "mep": "engineer",
                  "geotech": "engineer", "surveyor": "surveyor", "environmental": "environmental", "qd": "consultant",
                  "gc": "contractor", "owners_rep": "consultant", "attorney": "law"}

    class Meta:
        ordering = ["role", "firm"]

    def __str__(self):
        return f"{self.firm} ({self.get_role_display()})"

    def save(self, *args, **kwargs):
        self.firm = " ".join((self.firm or "").split())
        if self.firm and (self.directory is None or self.directory.name.lower() != self.firm.lower()):
            self.directory = Firm.objects.filter(name__iexact=self.firm).first() or Firm.objects.create(
                name=self.firm, kind=self.FIRM_KINDS.get(self.role, Firm.Kind.OTHER))
        super().save(*args, **kwargs)


class Bid(models.Model):
    """A general contractor's proposal for a project: the price, fee, general conditions and schedule, where it stands
    in the selection (invited → interviewed → shortlisted → selected), strengths and concerns. Upload the proposal and
    Claude levels the bids side by side, citing each one."""

    class Status(models.TextChoices):
        INVITED = "invited", "Invited"
        DECLINED = "declined", "Declined to bid"
        RECEIVED = "received", "Proposal received"
        INTERVIEWED = "interviewed", "Interviewed"
        SHORTLISTED = "shortlisted", "Shortlisted"
        SELECTED = "selected", "Selected"
        NOT_SELECTED = "not_selected", "Not selected"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="bids")
    firm = models.CharField(max_length=200)
    contact = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.INVITED)
    amount = models.DecimalField("price ($)", max_digits=14, decimal_places=2, null=True, blank=True,
                                 help_text="Lump sum or GMP as proposed")
    fee_pct = models.DecimalField("fee (%)", max_digits=5, decimal_places=2, null=True, blank=True)
    general_conditions = models.DecimalField("general conditions ($)", max_digits=14, decimal_places=2, null=True,
                                             blank=True)
    duration_months = models.DecimalField("schedule (months)", max_digits=5, decimal_places=1, null=True, blank=True)
    received_on = models.DateField(null=True, blank=True)
    interview_on = models.DateField(null=True, blank=True)
    strengths = models.TextField(blank=True)
    concerns = models.TextField(blank=True)
    file = models.FileField("proposal", upload_to="bids/%Y/%m/", blank=True)
    text = models.TextField(blank=True, editable=False)
    page_starts = models.JSONField(default=list, blank=True, editable=False)
    pages = models.PositiveIntegerField(default=0)
    text_status = models.CharField(max_length=12, blank=True)
    updated_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = [models.F("amount").asc(nulls_last=True), "firm"]

    def __str__(self):
        return self.firm

    @property
    def title(self):  # what citations call the proposal
        return f"{self.firm} proposal"

    @property
    def href(self):
        return reverse("crm:bid_file", args=[self.pk]) if self.file else ""

    def page_at(self, index):
        from bisect import bisect_right

        return max(1, bisect_right(self.page_starts or [0], index))


class ProjectAnswer(models.Model):
    """Claude's cited answer about a project's GC proposals: the bid leveling, or a question asked about them. Same
    block format as ContractAnswer."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="answers")
    question = models.TextField()
    blocks = models.JSONField(default=list, blank=True)
    documents = models.JSONField(default=list, blank=True)
    model = models.CharField(max_length=60, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    asked_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.question[:60]


class BudgetLine(models.Model):
    """A line of a project's budget (land, sitework, shell, MEP, soft costs, contingency): the original budget, what's
    committed and paid, and the forecast at completion. Approved change orders on the line add to its budget."""

    class Group(models.TextChoices):
        LAND = "land", "Land"
        HARD = "hard", "Hard costs"
        SOFT = "soft", "Soft costs"
        FINANCE = "finance", "Financing"
        CONTINGENCY = "contingency", "Contingency"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="budget")
    group = models.CharField(max_length=12, choices=Group.choices, default=Group.HARD)
    code = models.CharField(max_length=20, blank=True)
    name = models.CharField(max_length=120)
    original = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    committed = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True,
                                    help_text="Under contract or purchase order")
    paid = models.DecimalField("paid to date", max_digits=14, decimal_places=2, null=True, blank=True)
    forecast = models.DecimalField("forecast at completion", max_digits=14, decimal_places=2, null=True, blank=True,
                                   help_text="Blank = the current budget")
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.code} {self.name}".strip()


class ChangeOrder(models.Model):
    """A change to the work: what and why, the cost and days it adds, the budget line it lands on, and whether it's
    approved. Approved change orders add to their line's current budget."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    class Reason(models.TextChoices):
        OWNER = "owner", "Owner / tenant change"
        UNFORESEEN = "unforeseen", "Unforeseen condition"
        DESIGN = "design", "Design error or omission"
        AGENCY = "agency", "Code or agency requirement"
        OTHER = "other", "Other"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="change_orders")
    number = models.CharField(max_length=20, blank=True)
    title = models.CharField(max_length=200)
    reason = models.CharField(max_length=12, choices=Reason.choices, default=Reason.OTHER)
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0, help_text="Negative for a credit")
    days = models.IntegerField("schedule days", default=0)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING)
    line = models.ForeignKey(BudgetLine, null=True, blank=True, on_delete=models.SET_NULL, related_name="change_orders")
    submitted_on = models.DateField(null=True, blank=True)
    decided_on = models.DateField(null=True, blank=True)
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["submitted_on", "id"]

    def __str__(self):
        return f"CO {self.number} {self.title}".strip()


class Draw(models.Model):
    """A construction draw or pay application: the period, what was requested and funded, and retainage held."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        FUNDED = "funded", "Funded"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="draws")
    number = models.PositiveSmallIntegerField(default=1)
    period_end = models.DateField(null=True, blank=True)
    requested = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    funded = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    retainage = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_on = models.DateField(null=True, blank=True)
    funded_on = models.DateField(null=True, blank=True)
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["number", "id"]

    def __str__(self):
        return f"Draw {self.number}"


# ---------------------------------------------------------------------------
# Tenants (crm/tenants/): what a tenant's program needs from a site, and the terms of signed leases
# ---------------------------------------------------------------------------


class TenantNeeds(models.Model):
    """What a tenant's program needs from a site beyond the deal's acres, square feet and MW: the explosives (QD)
    cases its buildings carry, and rail or gas service. Drives the site fit ranking (crm/tenants/fit.py)."""

    opportunity = models.OneToOneField(Opportunity, on_delete=models.CASCADE, related_name="needs")
    qd_cases = models.JSONField(default=list, blank=True,
                                help_text='[{"name", "hd", "newqd", "kind", "sqft"}], one per explosives building')
    needs_rail = models.BooleanField(default=False)
    needs_gas = models.BooleanField("needs natural gas", default=False)
    notes = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Needs: {self.opportunity}"


class LeaseTerms(models.Model):
    """The business terms of a lease, for the rent roll: premises, dates, rent and escalations, renewal options,
    security and TI. Typed in, or read from the lease (as amended) by Claude, keeping the language behind each term
    in `sources` ({field: {"doc", "quote", "start", "end"}})."""

    contract = models.OneToOneField(Contract, on_delete=models.CASCADE, related_name="lease_terms")
    premises_sf = models.PositiveIntegerField("premises (sf)", null=True, blank=True)
    land_acres = models.DecimalField("land (acres)", max_digits=10, decimal_places=2, null=True, blank=True)
    commencement = models.DateField(null=True, blank=True)
    rent_start = models.DateField("rent commencement", null=True, blank=True)
    expiration = models.DateField(null=True, blank=True)
    base_rent = models.DecimalField("annual base rent ($)", max_digits=14, decimal_places=2, null=True, blank=True,
                                    help_text="In the first lease year")
    escalation_pct = models.DecimalField("annual escalation (%)", max_digits=5, decimal_places=2, null=True,
                                         blank=True)
    free_months = models.PositiveSmallIntegerField("free rent (months)", null=True, blank=True)
    renewals = models.PositiveSmallIntegerField("renewal options", null=True, blank=True)
    renewal_years = models.DecimalField("years each", max_digits=4, decimal_places=1, null=True, blank=True)
    renewal_notice_months = models.PositiveSmallIntegerField("renewal notice (months before expiration)", null=True,
                                                             blank=True)
    security_deposit = models.DecimalField("security deposit / LC ($)", max_digits=14, decimal_places=2, null=True,
                                           blank=True)
    ti_allowance = models.DecimalField("TI allowance ($)", max_digits=14, decimal_places=2, null=True, blank=True)
    guarantor = models.CharField(max_length=200, blank=True)
    sources = models.JSONField(default=dict, blank=True)
    updated_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Lease terms: {self.contract}"


# ---------------------------------------------------------------------------
# Landowner letters (crm/land/letters.py): mail-merged letters to a site's owners, and a log of what went out
# ---------------------------------------------------------------------------


class LetterTemplate(models.Model):
    """A landowner letter's text with merge fields: {owner}, {property}, {parcels}, {acres}, {county}, {site},
    {date}, {sender}, {sender_title}, {sender_phone}, {sender_email}. Built-in ones are kept by crm/land/letters.py;
    the team edits copies or adds its own."""

    key = models.CharField(max_length=40, blank=True, db_index=True, help_text="Built-in template key; blank = team-made")
    name = models.CharField(max_length=120)
    body = models.TextField()
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["key"], condition=~models.Q(key=""),
                                               name="letter_template_key_unique")]

    def __str__(self):
        return self.name


class LandownerLetter(models.Model):
    """One letter to one owner about a site: the name and address it went to, the parcels it was about, the template,
    the letter as sent, and when and how it went. Logging letters as sent also dates "Owner contacted"."""

    class Method(models.TextChoices):
        MAIL = "mail", "First-class mail"
        CERTIFIED = "certified", "Certified mail"
        HAND = "hand", "Hand-delivered"
        EMAIL = "email", "Email"

    site = models.ForeignKey(LandSite, on_delete=models.CASCADE, related_name="letters")
    owner_name = models.CharField(max_length=255)
    mailing = models.CharField(max_length=400, help_text="The address block it went to")
    parcels = models.JSONField(default=list, blank=True, help_text="Parcel ids the letter was about")
    template = models.ForeignKey(LetterTemplate, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    body = models.TextField(blank=True, help_text="The letter as it went out, merge fields filled in")
    method = models.CharField(max_length=10, choices=Method.choices, default=Method.MAIL)
    sent_on = models.DateField(default=timezone.localdate)
    sent_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    notes = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sent_on", "-id"]

    def __str__(self):
        return f"Letter to {self.owner_name} ({self.sent_on})"


class ChoiceOption(models.Model):
    """An edit to one option of a dropdown list (Settings → Dropdown lists, crm/choices.py): a new label, position or
    hidden flag for a built-in option, or an option added by the team."""

    list_key = models.CharField(max_length=80, db_index=True, help_text="The list, e.g. LandSite.Status")
    value = models.CharField(max_length=40)
    label = models.CharField(max_length=200, blank=True)
    position = models.PositiveSmallIntegerField(null=True, blank=True)
    hidden = models.BooleanField(default=False, help_text="Not offered for new records")
    added = models.BooleanField(default=False, help_text="Added by the team (not built in)")
    behaves_as = models.CharField(max_length=40, blank=True,
                                  help_text="Added options on logic lists: the built-in option it works like")
    updated_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["list_key", "position", "id"]
        constraints = [models.UniqueConstraint(fields=["list_key", "value"], name="choice_option_unique")]

    def __str__(self):
        return f"{self.list_key}: {self.label or self.value}"


# ---------------------------------------------------------------------------
# Capitalization (crm/capital/): each project's capital stack, the lenders and investors we're talking to, and their
# term sheets. Sources, terms and conversations only; the pro forma stays in Excel. Lenders, brokers and investors
# live in the directory (Firm), never as Companies or People, so they can't reach outreach.
# ---------------------------------------------------------------------------


class Firm(models.Model):
    """The directory: firms ACMI works with but never markets to. Lenders, brokers and investors (capital) and the
    lawyers, engineers, consultants, contractors and vendors (services). Kept apart from Companies and People, so
    nothing here is ever in a send queue, list or report. Its email domains and contacts let emails and meetings with
    it be found; the mail sync files its email here (FirmEmail) instead of the company review queue."""

    class Kind(models.TextChoices):
        BANK = "bank", "Bank"
        DEBT_FUND = "debt_fund", "Debt fund / private lender"
        BROKER = "broker", "Debt broker / advisor"
        INVESTOR = "investor", "Equity investor"
        AGENCY = "agency", "Agency / public program"
        LAW = "law", "Law firm"
        ARCHITECT = "architect", "Architect"
        ENGINEER = "engineer", "Engineer"
        ENVIRONMENTAL = "environmental", "Environmental consultant"
        SURVEYOR = "surveyor", "Surveyor"
        CONTRACTOR = "contractor", "Contractor / builder"
        CONSULTANT = "consultant", "Consultant / advisor"
        RE_BROKER = "re_broker", "Real estate broker"
        TITLE = "title", "Title / escrow"
        ACCOUNTING = "accounting", "Accountant / tax"
        INSURANCE = "insurance", "Insurance"
        VENDOR = "vendor", "Vendor / supplier"
        OTHER = "other", "Other"

    # The capital side of the directory: offered when a capital source names its provider.
    CAPITAL_KINDS = ("bank", "debt_fund", "broker", "investor", "agency")

    name = models.CharField(max_length=200, unique=True)
    kind = models.CharField(max_length=14, choices=Kind.choices, default=Kind.OTHER)
    domains = models.JSONField(default=list, blank=True, help_text="Email domains, e.g. truist.com")
    contacts = models.JSONField(default=list, blank=True, help_text="[{name, title, email, phone}]")
    website = models.URLField(blank=True)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("crm:firm", args=[self.pk])

    def save(self, *args, **kwargs):
        self.domains = sorted({d.strip().lower().lstrip("@") for d in self.domains or [] if d.strip()})
        super().save(*args, **kwargs)

    @property
    def emails(self):
        return [c["email"].lower() for c in self.contacts or [] if c.get("email")]

    @property
    def is_capital(self):
        from . import choices

        return choices.base("Firm.Kind", self.kind) in self.CAPITAL_KINDS


class CapitalSource(models.Model):
    """One piece of a project's capital stack and who might provide it: Fortress's equity, a senior construction loan
    from a bank, mezz, C-PACE, a state grant. Where the conversation stands (identified → in discussion → term sheet →
    committee / approved → closed, or passed), who has the ball, what's next, the term sheets (TermSheet) and the
    signed agreement (a Contract)."""

    class Kind(models.TextChoices):
        EQUITY = "equity", "Equity (JV / LP)"
        PREF = "pref", "Preferred equity"
        MEZZ = "mezz", "Mezzanine debt"
        CONSTRUCTION = "construction", "Construction loan"
        SENIOR = "senior", "Senior debt"
        BRIDGE = "bridge", "Bridge loan"
        PERM = "perm", "Permanent loan"
        CPACE = "cpace", "C-PACE"
        BONDS = "bonds", "Bonds / IRB"
        SELLER = "seller", "Seller financing"
        GRANT = "grant", "Grant / incentive"
        TAX_CREDIT = "tax_credit", "Tax credits"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        IDENTIFIED = "identified", "Identified"
        OUTREACH = "outreach", "Reached out"
        DISCUSSION = "discussion", "In discussion"
        TERM_SHEET = "term_sheet", "Term sheet"
        APPROVED = "approved", "Committee / approved"
        CLOSING = "closing", "Docs / closing"
        CLOSED = "closed", "Closed / committed"
        PASSED = "passed", "We passed"
        DECLINED = "declined", "They passed"

    STATUS_ORDER = Order("CapitalSource.Status",
                         ["identified", "outreach", "discussion", "term_sheet", "approved", "closing", "closed"])
    ACTIVE = Group("CapitalSource.Status", ["identified", "outreach", "discussion", "term_sheet", "approved", "closing"])
    DEBT = Group("CapitalSource.Kind", ["mezz", "construction", "senior", "bridge", "perm", "cpace", "bonds", "seller"])
    EQUITY = Group("CapitalSource.Kind", ["equity", "pref"])

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="capital_sources")
    kind = models.CharField(max_length=14, choices=Kind.choices, default=Kind.SENIOR)
    provider = models.ForeignKey(Firm, on_delete=models.PROTECT, related_name="sources",
                                 help_text="The lender, investor or agency")
    label = models.CharField(max_length=200, blank=True, help_text="e.g. Phase 1 construction loan")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.IDENTIFIED, db_index=True)
    status_since = models.DateField(null=True, blank=True)
    amount_sought = models.DecimalField("sought ($)", max_digits=14, decimal_places=2, null=True, blank=True)
    amount_committed = models.DecimalField("committed ($)", max_digits=14, decimal_places=2, null=True, blank=True)
    contact = models.CharField(max_length=200, blank=True, help_text="Our contact there")
    owner = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="capital_sources",
                              help_text="Who on the team runs it; gets the reminders")
    waiting_on = models.CharField("ball in court", max_length=6, choices=CampusUpdate.Waiting.choices, blank=True)
    waiting_since = models.DateField(null=True, blank=True)
    next_step = models.CharField(max_length=300, blank=True)
    next_step_on = models.DateField("next step by", null=True, blank=True)
    contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="capital_sources", verbose_name="signed agreement",
                                 help_text="The loan or JV agreement, once it's papered")
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["project", "kind", "id"]

    def __str__(self):
        return f"{self.provider} · {self.label or self.get_kind_display()}"

    def get_absolute_url(self):
        return reverse("crm:capital_source", args=[self.pk])

    @property
    def is_active(self):
        return self.status in self.ACTIVE

    @property
    def is_debt(self):
        return self.kind in self.DEBT


class TermSheet(models.Model):
    """A term sheet, LOI, quote or commitment letter from a lender or investor, one per version: the file (text read
    for Claude) and the key terms, typed or read by Claude with the language behind each (`sources`)."""

    class Kind(models.TextChoices):
        QUOTE = "quote", "Indicative quote"
        TERM_SHEET = "term_sheet", "Term sheet"
        LOI = "loi", "LOI"
        COMMITMENT = "commitment", "Commitment letter"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        COUNTERED = "countered", "We marked it up"
        ACCEPTED = "accepted", "Signed / accepted"
        DECLINED = "declined", "Declined"
        EXPIRED = "expired", "Expired"

    source = models.ForeignKey(CapitalSource, on_delete=models.CASCADE, related_name="term_sheets")
    version = models.PositiveSmallIntegerField(default=1)
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.TERM_SHEET)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RECEIVED)
    title = models.CharField(max_length=300, blank=True)
    dated = models.DateField("received / dated", null=True, blank=True)
    file = models.FileField(upload_to="capital/%Y/%m/", blank=True)
    text = models.TextField(blank=True, editable=False)
    page_starts = models.JSONField(default=list, blank=True, editable=False)
    pages = models.PositiveIntegerField(default=0)
    text_status = models.CharField(max_length=12, blank=True)
    # The key terms (capital/logic.py TERMS lists them in the order the comparison shows them)
    amount = models.DecimalField("amount ($)", max_digits=14, decimal_places=2, null=True, blank=True)
    rate = models.CharField("rate", max_length=200, blank=True, help_text="As quoted, e.g. SOFR + 3.25%, 7.1% fixed")
    spread_bps = models.DecimalField("spread (bps)", max_digits=7, decimal_places=1, null=True, blank=True)
    fixed_rate_pct = models.DecimalField("fixed rate (%)", max_digits=6, decimal_places=3, null=True, blank=True)
    floor_pct = models.DecimalField("rate floor (%)", max_digits=6, decimal_places=3, null=True, blank=True)
    term_months = models.PositiveSmallIntegerField("term (months)", null=True, blank=True)
    extensions = models.CharField(max_length=300, blank=True, help_text="e.g. two 12-month options, 25 bps each")
    amortization = models.CharField(max_length=200, blank=True)
    ltc_pct = models.DecimalField("LTC (%)", max_digits=6, decimal_places=2, null=True, blank=True)
    ltv_pct = models.DecimalField("LTV (%)", max_digits=6, decimal_places=2, null=True, blank=True)
    dscr_min = models.DecimalField("min DSCR (x)", max_digits=5, decimal_places=2, null=True, blank=True)
    debt_yield_pct = models.DecimalField("min debt yield (%)", max_digits=6, decimal_places=2, null=True, blank=True)
    origination_fee_pct = models.DecimalField("origination fee (%)", max_digits=6, decimal_places=3, null=True,
                                              blank=True)
    exit_fee_pct = models.DecimalField("exit fee (%)", max_digits=6, decimal_places=3, null=True, blank=True)
    other_fees = models.CharField(max_length=300, blank=True)
    recourse = models.CharField(max_length=300, blank=True, help_text="e.g. non-recourse with carve-outs; 25% repayment")
    guaranty = models.CharField("guaranties", max_length=300, blank=True)
    covenants = models.TextField(blank=True)
    prepayment = models.CharField(max_length=300, blank=True)
    interest_reserve = models.CharField(max_length=300, blank=True)
    equity_required = models.CharField("equity required", max_length=300, blank=True)
    pref_return_pct = models.DecimalField("preferred return (%)", max_digits=6, decimal_places=2, null=True,
                                          blank=True)
    promote = models.CharField("promote / waterfall", max_length=300, blank=True)
    deposit = models.DecimalField("good-faith deposit ($)", max_digits=12, decimal_places=2, null=True, blank=True)
    conditions = models.TextField("conditions to close", blank=True)
    expires_on = models.DateField("offer expires", null=True, blank=True)
    close_by = models.DateField("close by", null=True, blank=True, help_text="Outside closing date it sets")
    notes = models.TextField(blank=True)
    sources = models.JSONField(default=dict, blank=True, help_text="{field: {quote, start, end, page, note}} from Claude")
    read_at = models.DateTimeField(null=True, blank=True)
    added_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["source", "version", "id"]

    def __str__(self):
        return self.title or f"{self.get_kind_display()} v{self.version}"

    def get_absolute_url(self):
        return self.source.get_absolute_url() + f"#ts-{self.pk}"

    @property
    def href(self):
        return reverse("crm:term_sheet_file", args=[self.pk]) if self.file else ""

    def page_at(self, index):
        from bisect import bisect_right

        return max(1, bisect_right(self.page_starts or [0], index))


class CapitalLog(models.Model):
    """What happened with a capital source, newest first: calls and notes typed in, and status changes and term
    sheets logged by the app. Emails and Granola meetings with the provider show beside these, found live."""

    class Kind(models.TextChoices):
        NOTE = "note", "Note"
        CALL = "call", "Call"
        MEETING = "meeting", "Meeting"
        STATUS = "status", "Status change"
        TERM_SHEET = "term_sheet", "Term sheet"

    source = models.ForeignKey(CapitalSource, on_delete=models.CASCADE, related_name="log")
    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.NOTE)
    happened_on = models.DateField(default=timezone.localdate)
    text = models.TextField()
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-happened_on", "-id"]

    def __str__(self):
        return self.text[:60]


class FirmEmail(models.Model):
    """An email with someone at a directory firm's domain. The mail sync files these here instead of the review
    queue, so a lender's or a consultant's domain is never offered as a new company."""

    provider = models.ForeignKey(Firm, on_delete=models.CASCADE, related_name="emails_log")
    message_id = models.CharField(max_length=500)
    thread_id = models.CharField(max_length=100, blank=True)
    occurred_at = models.DateTimeField(db_index=True)
    kind = models.CharField(max_length=20, choices=Activity.Kind.choices)
    address = models.EmailField(max_length=255)
    name = models.CharField(max_length=255, blank=True)
    subject = models.CharField(max_length=500, blank=True)
    snippet = models.TextField(blank=True)
    mailbox = models.ForeignKey("Mailbox", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-occurred_at"]
        constraints = [
            models.UniqueConstraint(fields=["message_id", "address"], name="capital_email_unique"),
        ]

    def __str__(self):
        return f"{self.address}: {self.subject}"


class DomainSort(models.Model):
    """Claude's read of who's behind an email domain, from the mail itself (signatures, subjects, the domain): a
    prospect (a Company), a firm for the directory (with its type) or neither. For a domain waiting in the mail review
    queue, a clear directory firm is filed automatically (status Filed, with Undo); anything else is a suggestion on
    Needs filing. For an existing Company (company set), a suggestion that it's really a vendor, which a Manager acts
    on with Move to directory."""

    class Verdict(models.TextChoices):
        DIRECTORY = "directory", "Directory firm"
        COMPANY = "company", "Prospect (Company)"
        IGNORE = "ignore", "Neither (newsletter, personal, one-off)"
        UNSURE = "unsure", "Not sure"

    class Confidence(models.TextChoices):
        HIGH = "high", "High"
        MEDIUM = "medium", "Medium"
        LOW = "low", "Low"

    class Status(models.TextChoices):
        SUGGESTED = "suggested", "Suggested"
        FILED = "filed", "Filed by Claude"
        UNDONE = "undone", "Undone"
        DONE = "done", "Acted on"
        DISMISSED = "dismissed", "Dismissed"

    domain = models.CharField(max_length=255, db_index=True)
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.CASCADE, related_name="sorts")
    verdict = models.CharField(max_length=10, choices=Verdict.choices)
    kind = models.CharField(max_length=14, blank=True, help_text="The directory type, for a directory firm")
    name = models.CharField(max_length=200, blank=True, help_text="The organization's name as the mail gives it")
    confidence = models.CharField(max_length=6, choices=Confidence.choices, default=Confidence.LOW)
    reason = models.CharField(max_length=400, blank=True)
    contacts = models.JSONField(default=list, blank=True, help_text="[{name, title, email, phone}] from signatures")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SUGGESTED)
    firm = models.ForeignKey(Firm, null=True, blank=True, on_delete=models.SET_NULL, related_name="sorts")
    created_firm = models.BooleanField(default=False, help_text="The firm was started by this filing")
    decided_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.domain}: {self.get_verdict_display()}"

    @property
    def kind_label(self):
        return dict(Firm.Kind.choices).get(self.kind, self.kind)


# ---------------------------------------------------------------------------
# Boards: Trello-style lists and cards on top of the CRM's records (crm/boards/). A card is a view of a record
# wherever one exists; a card with no record is allowed and can be linked later.
# ---------------------------------------------------------------------------

BOARD_COLOURS = [("navy", "Navy"), ("sky", "Sky"), ("teal", "Teal"), ("green", "Green"), ("amber", "Amber"),
                 ("rose", "Rose"), ("violet", "Violet"), ("slate", "Slate")]
# Trello's label palette. The label's name is always shown next to its colour.
LABEL_COLOURS = [("green", "Green"), ("yellow", "Yellow"), ("orange", "Orange"), ("red", "Red"), ("purple", "Purple"),
                 ("blue", "Blue"), ("sky", "Sky"), ("lime", "Lime"), ("pink", "Pink"), ("black", "Black")]


class Board(models.Model):
    class Kind(models.TextChoices):
        PROJECT = "project", "Project board"
        CAMPUS = "campus", "Campus board"
        TEAM = "team", "Team board"
        CUSTOM = "custom", "Custom board"
        BOUND = "bound", "Record board"  # lists bound to a record type's statuses (crm/boards/bound.py)

    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.CUSTOM)
    record_type = models.CharField(max_length=20, blank=True,
                                   help_text="On a record board: the record its bound lists hold (opportunity, site…)")
    campus = models.ForeignKey(Campus, null=True, blank=True, on_delete=models.SET_NULL, related_name="boards")
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="boards")
    description = models.TextField(blank=True)
    colour = models.CharField(max_length=12, choices=BOARD_COLOURS, default="navy")
    position = models.PositiveIntegerField(default=0)
    archived = models.BooleanField(default=False, db_index=True)
    members = models.ManyToManyField(User, blank=True, related_name="boards",
                                     help_text="Shown under My boards; everyone signed in can open any board")
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("crm:board", args=[self.pk])

    @property
    def home_label(self):
        return " / ".join(str(x) for x in [self.campus, self.project] if x)


class BoardList(models.Model):
    """A Trello list: one column of a board."""

    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="lists")
    name = models.CharField(max_length=120)
    position = models.IntegerField(default=0)
    archived = models.BooleanField(default=False)
    maps_to = models.CharField(max_length=40, blank=True,
                               help_text="On a record board: the status value this list holds")
    feed = models.CharField(max_length=20, blank=True,
                            help_text="A read-only list filled from the board's records: critical_dates, open_tasks…")

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.name


class BoardLabel(models.Model):
    board = models.ForeignKey(Board, on_delete=models.CASCADE, related_name="labels")
    name = models.CharField(max_length=60)
    colour = models.CharField(max_length=12, choices=LABEL_COLOURS, default="blue")
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.name


class Card(models.Model):
    """One card. `list` names the column (as ListEntry.list does); `record()` is the CRM record it stands for."""

    RECORD_FIELDS = ("opportunity", "site", "contract", "approval", "capital_source", "firm", "company", "meeting", "task")
    RECORD_LABELS = {"opportunity": "Tenant deal", "site": "Site", "contract": "Contract", "approval": "Entitlement",
                     "capital_source": "Capital source", "firm": "Directory", "company": "Company", "meeting": "Meeting",
                     "task": "Task"}

    list = models.ForeignKey(BoardList, on_delete=models.CASCADE, related_name="cards")
    position = models.IntegerField(default=0)
    title = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    start = models.DateField(null=True, blank=True)
    due = models.DateField(null=True, blank=True, db_index=True)
    due_time = models.TimeField(null=True, blank=True)
    done = models.BooleanField(default=False)
    done_at = models.DateTimeField(null=True, blank=True)
    cover = models.CharField(max_length=12, choices=LABEL_COLOURS, blank=True)
    archived = models.BooleanField(default=False, db_index=True)
    labels = models.ManyToManyField(BoardLabel, blank=True, related_name="cards")
    members = models.ManyToManyField(User, blank=True, related_name="cards")
    company = models.ForeignKey(Company, null=True, blank=True, on_delete=models.SET_NULL, related_name="cards")
    opportunity = models.ForeignKey(Opportunity, null=True, blank=True, on_delete=models.SET_NULL, related_name="cards")
    site = models.ForeignKey(LandSite, null=True, blank=True, on_delete=models.SET_NULL, related_name="cards")
    contract = models.ForeignKey(Contract, null=True, blank=True, on_delete=models.SET_NULL, related_name="cards")
    firm = models.ForeignKey(Firm, null=True, blank=True, on_delete=models.SET_NULL, related_name="cards")
    approval = models.ForeignKey(Approval, null=True, blank=True, on_delete=models.SET_NULL, related_name="cards")
    capital_source = models.ForeignKey(CapitalSource, null=True, blank=True, on_delete=models.SET_NULL, related_name="cards")
    meeting = models.ForeignKey(Meeting, null=True, blank=True, on_delete=models.SET_NULL, related_name="cards")
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.SET_NULL, related_name="cards")
    source_key = models.CharField(max_length=120, null=True, blank=True, unique=True, editable=False,
                                  help_text="Import key (trello:<card id>) so a re-run updates instead of duplicating")
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse("crm:board", args=[self.list.board_id]) + f"?card={self.pk}"

    @property
    def board(self):
        return self.list.board

    def record_field(self):
        return next((f for f in self.RECORD_FIELDS if getattr(self, f + "_id")), "")

    def record(self):
        field = self.record_field()
        return getattr(self, field) if field else None

    @property
    def record_kind(self):
        return self.RECORD_LABELS.get(self.record_field(), "")

    @property
    def due_state(self):
        """"late", "today", "soon" (within 7 days) or "" for the due chip; nothing once done."""
        if self.done or not self.due:
            return ""
        days = (self.due - timezone.localdate()).days
        return "late" if days < 0 else "today" if days == 0 else "soon" if days <= 7 else ""


class Checklist(models.Model):
    card = models.ForeignKey(Card, on_delete=models.CASCADE, related_name="checklists")
    name = models.CharField(max_length=120, default="Checklist")
    position = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.name


class ChecklistItem(models.Model):
    checklist = models.ForeignKey(Checklist, on_delete=models.CASCADE, related_name="items")
    text = models.CharField(max_length=300)
    done = models.BooleanField(default=False)
    due = models.DateField(null=True, blank=True)
    assignee = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    position = models.PositiveIntegerField(default=0)
    task = models.ForeignKey(Task, null=True, blank=True, on_delete=models.SET_NULL, related_name="checklist_items",
                             help_text="The real task this item became (reminders, My tasks)")

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.text


class CardAttachment(models.Model):
    card = models.ForeignKey(Card, on_delete=models.CASCADE, related_name="attachments")
    name = models.CharField(max_length=300)
    url = models.URLField(max_length=1000, blank=True, help_text="A Drive (or any) link")
    file = models.FileField(upload_to="cards/%Y/%m/", blank=True)
    added_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-added_at"]

    def __str__(self):
        return self.name


class CardEvent(models.Model):
    """The card's activity log: created, moved, renamed, label, member, due, done, archived, linked."""

    card = models.ForeignKey(Card, on_delete=models.CASCADE, related_name="events")
    when = models.DateTimeField(default=timezone.now, db_index=True)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    kind = models.CharField(max_length=12)
    text = models.CharField(max_length=300)

    class Meta:
        ordering = ["-when", "-id"]

    def __str__(self):
        return self.text


# ---------------------------------------------------------------------------
# Data room (crm/dataroom/): what capital partners see, with their own accounts
# ---------------------------------------------------------------------------


class DataRoom(models.Model):
    """A capital partner's read-only view of the pipeline: the deals on the investor report (each can be switched
    off), a company card per deal with only the fields in `crm/dataroom/logic.py`, and the files ACMI approved."""

    name = models.CharField(max_length=120, help_text="e.g. Fortress")
    partner = models.ForeignKey(Firm, null=True, blank=True, on_delete=models.SET_NULL, related_name="data_rooms")
    intro = models.TextField(blank=True, help_text="Shown to the partner at the top of the room")
    campuses = models.ManyToManyField(Campus, blank=True, related_name="+",
                                      help_text="Only deals at these campuses; none ticked means every campus")
    show_costs = models.BooleanField("show project cost and $ PSF", default=True)
    show_win = models.BooleanField("show win % and weighted cost", default=False)
    active = models.BooleanField(default=True, help_text="Off: nobody can sign in to this room")
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("crm:data_room_manage", args=[self.pk])


class DataRoomMember(models.Model):
    """One person at the partner. Invited by a Manager; the invite link lets them make their account (a password of
    their own) and later resets it. Switching a member off stops their sign-in at once."""

    room = models.ForeignKey(DataRoom, on_delete=models.CASCADE, related_name="members")
    user = models.OneToOneField(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="data_room_member")
    name = models.CharField(max_length=120)
    email = models.EmailField(unique=True)
    invite_hash = models.CharField(max_length=64, blank=True, db_index=True, editable=False)
    invite_expires = models.DateTimeField(null=True, blank=True)
    invited_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    invited_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} <{self.email}>"


class DataRoomDeal(models.Model):
    """A deal's switch in a room. No row: a deal on the investor report is shown."""

    room = models.ForeignKey(DataRoom, on_delete=models.CASCADE, related_name="deal_switches")
    opportunity = models.ForeignKey(Opportunity, on_delete=models.CASCADE, related_name="data_room_switches")
    shown = models.BooleanField(default=True)
    changed_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    changed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["room", "opportunity"], name="data_room_deal_unique")]


class DataRoomFile(models.Model):
    """A file ACMI approved for the room: one of the deal's documents, or a file uploaded for the room. Only these are
    ever served to the partner, and every download is logged."""

    room = models.ForeignKey(DataRoom, on_delete=models.CASCADE, related_name="files")
    opportunity = models.ForeignKey(Opportunity, null=True, blank=True, on_delete=models.CASCADE,
                                    related_name="data_room_files", help_text="Empty: shown at the top of the room")
    document = models.ForeignKey(Document, null=True, blank=True, on_delete=models.CASCADE, related_name="data_room_files")
    file = models.FileField(upload_to="dataroom/%Y/%m/", blank=True)
    drive_id = models.CharField(max_length=100, blank=True, help_text="A Drive file, read from Drive when downloaded")
    drive_name = models.CharField(max_length=300, blank=True, help_text="Its name in Drive when it was approved")
    drive_folder = models.CharField(max_length=300, blank=True, help_text="Its sub-folder in the mapped folder")
    drive_mime = models.CharField(max_length=120, blank=True)
    title = models.CharField(max_length=300)
    approved_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    approved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title

    @property
    def source_drive_id(self):
        """The Drive file served through the room: one approved from a Drive folder, or a document's Drive link."""
        if self.drive_id:
            return self.drive_id
        if self.document_id and self.document.url:
            from .drive import file_id_from

            return file_id_from(self.document.url)
        return ""

    @property
    def link(self):
        """A non-Drive link the partner opens directly; empty for files served through the room (Drive included,
        so a partner never sees a Drive address or folder)."""
        if self.document_id and self.document.url and not self.source_drive_id:
            return self.document.url
        return ""

    @property
    def stored_file(self):
        if self.file:
            return self.file
        return self.document.file if self.document_id and self.document.file else None


class DataRoomView(models.Model):
    """The room's log: every page a partner opened and every file they downloaded."""

    room = models.ForeignKey(DataRoom, on_delete=models.CASCADE, related_name="views")
    member = models.ForeignKey(DataRoomMember, null=True, blank=True, on_delete=models.SET_NULL, related_name="views")
    when = models.DateTimeField(default=timezone.now, db_index=True)
    page = models.CharField(max_length=20)  # home, deal, file, sign-in
    opportunity = models.ForeignKey(Opportunity, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    file = models.ForeignKey(DataRoomFile, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-when", "-id"]


class DataRoomLoginAttempt(models.Model):
    """A wrong password on the data room's sign-in page (crm/dataroom/logic.py). Kept in the database rather than a
    per-process cache, so the lockout holds across every server process and instance."""

    username = models.CharField(max_length=254, blank=True, db_index=True)
    ip = models.CharField(max_length=45, blank=True, db_index=True)
    at = models.DateTimeField(default=timezone.now, db_index=True)

    def __str__(self):
        return f"{self.username or '?'} from {self.ip or '?'} at {self.at:%Y-%m-%d %H:%M}"
