import uuid
from pathlib import Path

from django.conf import settings
from django.core.validators import FileExtensionValidator
from django.db import models


def user_upload(instance, filename):
    ext = Path(filename).suffix.lower()
    return f"users/{instance.owner_id}/{instance.__class__.__name__.lower()}/{uuid.uuid4().hex}{ext}"


class UUIDTimeModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        abstract = True


class UploadedDataset(UUIDTimeModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"; PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"; FAILED = "failed", "Failed"
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="datasets")
    original_file = models.FileField(upload_to=user_upload, validators=[FileExtensionValidator(["xlsx", "xls", "csv"])])
    original_filename = models.CharField(max_length=255)
    file_type = models.CharField(max_length=10)
    size = models.PositiveBigIntegerField(default=0)
    checksum = models.CharField(max_length=64)
    sheet_name = models.CharField(max_length=255, blank=True)
    header_row_number = models.PositiveIntegerField(default=1)
    detected_columns = models.JSONField(default=list)
    row_count = models.PositiveIntegerField(default=0)
    processing_status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    processing_error = models.TextField(blank=True)
    selected_phone_column = models.CharField(max_length=255, blank=True)


class SuppressedRecipient(UUIDTimeModel):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="suppressions")
    normalized_phone = models.CharField(max_length=20)
    reason = models.CharField(max_length=255, blank=True)
    source = models.CharField(max_length=100, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["owner", "normalized_phone"], name="unique_owner_suppression")]


class ImportedRecipient(UUIDTimeModel):
    class Validation(models.TextChoices):
        UNCHECKED = "unchecked", "Unchecked"; VALID = "valid", "Valid"
        WARNING = "warning", "Warning"; INVALID = "invalid", "Invalid"; BLANK = "blank", "Blank"
    class WhatsApp(models.TextChoices):
        UNKNOWN = "unknown", "Unknown"; CHECKING = "checking", "Checking"; EXISTS = "exists", "Exists"
        NOT_EXISTS = "not_exists", "Not on WhatsApp"; ERROR = "error", "Error"
    dataset = models.ForeignKey(UploadedDataset, on_delete=models.CASCADE, related_name="recipients")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="recipients")
    original_row_number = models.PositiveIntegerField()
    row_data = models.JSONField(default=dict)
    selected = models.BooleanField(default=True)
    phone_source_column = models.CharField(max_length=255, blank=True)
    phone_original = models.CharField(max_length=255, blank=True)
    phone_normalized = models.CharField(max_length=20, blank=True)
    phone_validation_status = models.CharField(max_length=20, choices=Validation.choices, default=Validation.UNCHECKED)
    validation_error_code = models.CharField(max_length=50, blank=True)
    validation_error_message = models.CharField(max_length=255, blank=True)
    auto_corrected = models.BooleanField(default=False)
    whatsapp_state = models.CharField(max_length=20, choices=WhatsApp.choices, default=WhatsApp.UNKNOWN)
    whatsapp_checked_at = models.DateTimeField(null=True, blank=True)
    duplicate_of = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="duplicates")
    suppressed = models.BooleanField(default=False)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["dataset", "original_row_number"], name="unique_dataset_row")]
        indexes = [models.Index(fields=["owner", "phone_normalized"]), models.Index(fields=["dataset", "selected"])]


class UploadedMedia(UUIDTimeModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"; UPLOADING = "uploading", "Uploading"
        READY = "ready", "Ready"; FAILED = "failed", "Failed"
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="media")
    original_file = models.FileField(upload_to=user_upload)
    original_filename = models.CharField(max_length=255)
    mime_type = models.CharField(max_length=100)
    media_type = models.CharField(max_length=20)
    size = models.PositiveBigIntegerField()
    checksum = models.CharField(max_length=64)
    provider_public_url = models.URLField(max_length=1000, blank=True)
    provider_url_expires_at = models.DateTimeField(null=True, blank=True)
    upload_status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    upload_error = models.TextField(blank=True)


class MessageTemplate(UUIDTimeModel):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="message_templates")
    name = models.CharField(max_length=150)
    body = models.TextField()
    detected_placeholders = models.JSONField(default=list)
    default_media = models.ForeignKey(UploadedMedia, null=True, blank=True, on_delete=models.SET_NULL)


class Campaign(UUIDTimeModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"; VALIDATING = "validating", "Validating"; CHECKING = "checking", "Checking"
        READY = "ready", "Ready"; QUEUED = "queued", "Queued"; SENDING = "sending", "Sending"
        PAUSED = "paused", "Paused"; COMPLETED = "completed", "Completed"
        ERRORS = "completed_with_errors", "Completed with errors"; CANCELLED = "cancelled", "Cancelled"
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="campaigns")
    dataset = models.ForeignKey(UploadedDataset, on_delete=models.PROTECT, related_name="campaigns")
    name = models.CharField(max_length=150)
    template = models.ForeignKey(MessageTemplate, null=True, blank=True, on_delete=models.SET_NULL)
    body_snapshot = models.TextField()
    selected_phone_column = models.CharField(max_length=255)
    selected_recipient_count = models.PositiveIntegerField(default=0)
    media = models.ForeignKey(UploadedMedia, null=True, blank=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.DRAFT)
    missing_value_policy = models.CharField(max_length=20, default="empty")
    missing_value_fallback = models.CharField(max_length=255, blank=True)
    allow_duplicates = models.BooleanField(default=False)
    allow_unknown = models.BooleanField(default=False)
    opt_in_confirmed = models.BooleanField(default=False)
    total_count = models.PositiveIntegerField(default=0)
    queued_count = models.PositiveIntegerField(default=0)
    sent_count = models.PositiveIntegerField(default=0)
    delivered_count = models.PositiveIntegerField(default=0)
    read_count = models.PositiveIntegerField(default=0)
    failed_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    send_config_snapshot = models.JSONField(default=dict)


class CampaignRecipient(UUIDTimeModel):
    class State(models.TextChoices):
        INVALID = "invalid", "Invalid"; SKIPPED = "skipped", "Skipped"; CANCELLED = "cancelled", "Cancelled"
        QUEUED = "queued", "Queued"; ACCEPTED = "accepted", "Provider accepted"; PENDING = "pending", "Pending"
        SENT = "sent", "Sent"; DELIVERED = "delivered", "Delivered"; READ = "read", "Read"
        PLAYED = "played", "Played"; FAILED = "failed", "Failed"
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name="campaign_recipients")
    imported_recipient = models.ForeignKey(ImportedRecipient, on_delete=models.PROTECT, related_name="campaign_entries")
    rendered_message = models.TextField(blank=True)
    normalized_phone = models.CharField(max_length=20, blank=True)
    state = models.CharField(max_length=20, choices=State.choices, default=State.QUEUED)
    skip_reason = models.CharField(max_length=255, blank=True)
    provider_message_id = models.CharField(max_length=255, blank=True)
    provider_jid = models.CharField(max_length=255, blank=True)
    provider_status_code = models.IntegerField(null=True, blank=True)
    provider_status_text = models.CharField(max_length=100, blank=True)
    last_provider_payload = models.JSONField(default=dict)
    queued_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    retry_count = models.PositiveIntegerField(default=0)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["campaign", "imported_recipient"], name="unique_campaign_recipient")]


class MessageAttempt(UUIDTimeModel):
    campaign_recipient = models.ForeignKey(CampaignRecipient, on_delete=models.CASCADE, related_name="attempts")
    attempt_number = models.PositiveIntegerField()
    request_payload = models.JSONField(default=dict)
    http_status = models.IntegerField(null=True, blank=True)
    provider_response = models.JSONField(default=dict)
    error_category = models.CharField(max_length=50, blank=True)
    error_message = models.TextField(blank=True)
    provider_message_id = models.CharField(max_length=255, blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    class Meta:
        constraints = [models.UniqueConstraint(fields=["campaign_recipient", "attempt_number"], name="unique_attempt_number")]


class WebhookEvent(UUIDTimeModel):
    event_hash = models.CharField(max_length=64, unique=True)
    event_id = models.CharField(max_length=255, blank=True)
    event_type = models.CharField(max_length=100, blank=True)
    signature_valid = models.BooleanField(default=False)
    payload = models.JSONField(default=dict)
    processing_state = models.CharField(max_length=20, default="pending")
    processing_error = models.TextField(blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)


class LiveIntegrationResult(UUIDTimeModel):
    phone_masked = models.CharField(max_length=30)
    check_http_status = models.IntegerField(null=True, blank=True)
    exists = models.BooleanField(null=True)
    send_http_status = models.IntegerField(null=True, blank=True)
    success = models.BooleanField(default=False)
    provider_message_id = models.CharField(max_length=255, blank=True)
    provider_state = models.CharField(max_length=100, blank=True)
    response_summary = models.JSONField(default=dict)
