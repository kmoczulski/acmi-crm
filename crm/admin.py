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
