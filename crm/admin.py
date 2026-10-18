from django.contrib import admin

from . import models


@admin.register(models.Company)
class CompanyAdmin(admin.ModelAdmin):
    list_display = ("name", "status", "priority", "sector", "owner", "touch_count", "last_interaction_at")
    list_filter = ("status", "priority", "owner")
    search_fields = ("name", "sector", "domains")


@admin.register(models.Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "company", "title", "priority", "owner", "last_response")
    list_filter = ("priority", "owner")
    search_fields = ("full_name", "email", "company__name")
    raw_id_fields = ("company",)


@admin.register(models.Activity)
class ActivityAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "kind", "summary", "company", "person", "outcome", "source")
    list_filter = ("kind", "source", "is_background", "campaign")
    search_fields = ("summary", "company__name", "person__email")
    raw_id_fields = ("company", "person", "opportunity")


@admin.register(models.Opportunity)
class OpportunityAdmin(admin.ModelAdmin):
    list_display = ("name", "company", "campus", "stage", "owner", "value")
    list_filter = ("stage", "campus", "owner")
    raw_id_fields = ("company", "people")


admin.site.register(models.Campus)
admin.site.register(models.Task)
admin.site.register(models.Note)
admin.site.register(models.List)
admin.site.register(models.ListEntry)
admin.site.register(models.SavedView)
admin.site.register(models.FieldHistory)
admin.site.register(models.EmailTemplate)
admin.site.register(models.NewsDraft)


@admin.register(models.Mailbox)
class MailboxAdmin(admin.ModelAdmin):
    list_display = ("email", "user", "enabled", "backfill_done", "last_uid", "last_synced_at", "last_error")


@admin.register(models.UnmatchedEmail)
class UnmatchedEmailAdmin(admin.ModelAdmin):
    list_display = ("occurred_at", "domain", "address", "kind", "subject")
    list_filter = ("kind",)
    search_fields = ("domain", "address", "subject")


admin.site.register(models.MailFilter)
admin.site.register(models.AISummary)
admin.site.register(models.DuplicateDismissal)
admin.site.register(models.Project)
admin.site.register(models.NewsScan)
admin.site.register(models.NewsItem)
admin.site.register(models.OutreachSettings)
admin.site.register(models.OutreachFile)
admin.site.register(models.SendBatch)
admin.site.register(models.SendItem)

# Google refresh tokens (Calendar read access) shouldn't be readable by anyone with admin access.
try:
    from allauth.socialaccount.models import SocialToken

    admin.site.unregister(SocialToken)
except (ImportError, admin.sites.NotRegistered):
    pass


@admin.register(models.CampusUpdate)
class CampusUpdateAdmin(admin.ModelAdmin):
    list_display = ("happened_on", "campus", "workstream", "text", "waiting_on", "author")
    list_filter = ("campus", "workstream", "waiting_on")
    search_fields = ("text",)


@admin.register(models.CampusReport)
class CampusReportAdmin(admin.ModelAdmin):
    list_display = ("week_of", "campus", "status", "sent_by", "sent_at")
    list_filter = ("campus", "status")


class ContractDocumentInline(admin.TabularInline):
    model = models.ContractDocument
    fields = ("kind", "title", "dated", "sent_by", "file", "pages", "text_status")
    readonly_fields = ("pages", "text_status")
    extra = 0


class ContractDateInline(admin.TabularInline):
    model = models.ContractDate
    fields = ("kind", "label", "date", "amount", "confirmed", "done")
    extra = 0


@admin.register(models.Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = ("title", "kind", "status", "counterparty", "site", "campus", "owner", "updated_at")
    list_filter = ("kind", "status", "campus")
    search_fields = ("title", "counterparty")
    raw_id_fields = ("site", "opportunity", "company")
    filter_horizontal = ("parcel_deals",)
    inlines = [ContractDocumentInline, ContractDateInline]


@admin.register(models.ContractTemplate)
class ContractTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "key", "updated_at")


@admin.register(models.DiligenceItem)
class DiligenceItemAdmin(admin.ModelAdmin):
    list_display = ("site", "ref", "name", "status", "consultant", "due_on", "budget", "cost")
    list_filter = ("status", "site")
    search_fields = ("name", "consultant", "findings")
    readonly_fields = ("summary",)


@admin.register(models.ClosingItem)
class ClosingItemAdmin(admin.ModelAdmin):
    list_display = ("contract", "side", "name", "status", "due_on", "done_on")
    list_filter = ("status", "side")


class ApprovalStepInline(admin.TabularInline):
    model = models.ApprovalStep
    fields = ("kind", "date", "body", "done")
    extra = 0


class ApprovalConditionInline(admin.TabularInline):
    model = models.ApprovalCondition
    fields = ("text", "timing", "due_on", "status", "confirmed")
    extra = 0


@admin.register(models.Approval)
class ApprovalAdmin(admin.ModelAdmin):
    list_display = ("title", "kind", "status", "jurisdiction", "project", "site", "campus", "owner", "updated_at")
    list_filter = ("kind", "status", "campus")
    search_fields = ("title", "jurisdiction", "case_number")
    raw_id_fields = ("site", "contract")
    inlines = [ApprovalStepInline, ApprovalConditionInline]


@admin.register(models.ApprovalDocument)
class ApprovalDocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "kind", "approval", "dated", "pages", "text_status")
    readonly_fields = ("summary",)


@admin.register(models.BuildMilestone)
class BuildMilestoneAdmin(admin.ModelAdmin):
    list_display = ("project", "phase", "name", "baseline", "forecast", "actual")
    list_filter = ("project", "phase")


@admin.register(models.Bid)
class BidAdmin(admin.ModelAdmin):
    list_display = ("project", "firm", "status", "amount", "fee_pct", "duration_months")
    list_filter = ("project", "status")


admin.site.register(models.TeamMember)
admin.site.register(models.BudgetLine)
admin.site.register(models.ChangeOrder)
admin.site.register(models.Draw)
admin.site.register(models.ProjectAnswer)
admin.site.register(models.TenantNeeds)


@admin.register(models.LeaseTerms)
class LeaseTermsAdmin(admin.ModelAdmin):
    list_display = ("contract", "premises_sf", "rent_start", "expiration", "base_rent", "escalation_pct")
    readonly_fields = ("sources",)


@admin.register(models.ContractAnswer)
class ContractAnswerAdmin(admin.ModelAdmin):
    list_display = ("created_at", "contract", "question", "asked_by", "input_tokens", "output_tokens")
    list_filter = ("contract",)
    readonly_fields = ("blocks", "documents")
