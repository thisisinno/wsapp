from django.contrib import admin
from .models import (Campaign, CampaignRecipient, ImportedRecipient, LiveIntegrationResult,
                     MessageAttempt, MessageTemplate, SuppressedRecipient, UploadedDataset,
                     UploadedMedia, WebhookEvent)


@admin.register(UploadedDataset)
class DatasetAdmin(admin.ModelAdmin):
    list_display = ("original_filename", "owner", "processing_status", "row_count", "created_at")
    list_filter = ("processing_status", "file_type")
    search_fields = ("original_filename", "owner__username", "checksum")
    readonly_fields = ("id", "size", "checksum", "created_at", "updated_at")


@admin.register(ImportedRecipient)
class RecipientAdmin(admin.ModelAdmin):
    list_display = ("phone_normalized", "owner", "phone_validation_status", "whatsapp_state", "selected", "suppressed")
    list_filter = ("phone_validation_status", "whatsapp_state", "selected", "suppressed")
    search_fields = ("phone_normalized", "phone_original", "owner__username")
    readonly_fields = ("row_data", "created_at", "updated_at")


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "status", "total_count", "sent_count", "failed_count", "created_at")
    list_filter = ("status",)
    search_fields = ("name", "owner__username")
    readonly_fields = ("send_config_snapshot", "body_snapshot", "created_at", "updated_at")


@admin.register(CampaignRecipient)
class CampaignRecipientAdmin(admin.ModelAdmin):
    list_display = ("normalized_phone", "campaign", "state", "provider_message_id", "retry_count")
    list_filter = ("state",)
    search_fields = ("normalized_phone", "provider_message_id", "campaign__name")
    readonly_fields = ("rendered_message", "last_provider_payload", "created_at", "updated_at")


@admin.register(MessageAttempt)
class AttemptAdmin(admin.ModelAdmin):
    list_display = ("campaign_recipient", "attempt_number", "http_status", "error_category", "created_at")
    readonly_fields = ("request_payload", "provider_response", "error_message", "created_at", "updated_at")


@admin.register(WebhookEvent)
class WebhookAdmin(admin.ModelAdmin):
    list_display = ("event_type", "event_id", "signature_valid", "processing_state", "received_at")
    list_filter = ("signature_valid", "processing_state", "event_type")
    readonly_fields = ("payload", "event_hash", "received_at", "processed_at")


admin.site.register([MessageTemplate, UploadedMedia, SuppressedRecipient, LiveIntegrationResult])
