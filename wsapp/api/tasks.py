import hashlib
import json
import random
import time
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from .models import Campaign, CampaignRecipient, ImportedRecipient, MessageAttempt, UploadedDataset, UploadedMedia, WebhookEvent
from .services.imports import parse_tabular
from .services.phones import normalize_tanzania_phone
from .services.templates import render_message
from .services.wasender import RateLimitError, WasenderClient, WasenderError


@shared_task
def process_dataset(dataset_id, sheet_name="", header_row=1):
    dataset = UploadedDataset.objects.get(pk=dataset_id)
    dataset.processing_status = UploadedDataset.Status.PROCESSING
    dataset.processing_error = ""
    dataset.save(update_fields=["processing_status", "processing_error", "updated_at"])
    try:
        with dataset.original_file.open("rb") as source:
            columns, rows = parse_tabular(source, dataset.original_filename, sheet_name, header_row)
        with transaction.atomic():
            dataset.recipients.all().delete()
            ImportedRecipient.objects.bulk_create([
                ImportedRecipient(dataset=dataset, owner=dataset.owner, original_row_number=n, row_data=row)
                for n, row in rows
            ], batch_size=1000)
            dataset.detected_columns, dataset.row_count = columns, len(rows)
            dataset.sheet_name, dataset.header_row_number = sheet_name, header_row
            dataset.processing_status = UploadedDataset.Status.READY
            dataset.save()
    except Exception as exc:
        dataset.processing_status = UploadedDataset.Status.FAILED
        dataset.processing_error = str(exc)[:1000]
        dataset.save(update_fields=["processing_status", "processing_error", "updated_at"])


@shared_task
def normalize_dataset(dataset_id, column):
    dataset = UploadedDataset.objects.get(pk=dataset_id)
    seen = {}
    suppressions = set(dataset.owner.suppressions.values_list("normalized_phone", flat=True))
    for recipient in dataset.recipients.order_by("original_row_number"):
        raw = recipient.row_data.get(column, "")
        result = normalize_tanzania_phone(raw)
        recipient.phone_source_column = column
        recipient.phone_original = result.original
        recipient.phone_normalized = result.normalized
        recipient.phone_validation_status = result.status
        recipient.validation_error_code = result.code
        recipient.validation_error_message = result.message
        recipient.auto_corrected = result.auto_corrected
        recipient.suppressed = bool(result.normalized in suppressions)
        recipient.duplicate_of = seen.get(result.normalized) if result.normalized else None
        recipient.save()
        if result.normalized and result.normalized not in seen:
            seen[result.normalized] = recipient
    dataset.selected_phone_column = column
    dataset.save(update_fields=["selected_phone_column", "updated_at"])


@shared_task(bind=True, max_retries=3)
def check_recipient(self, recipient_id):
    recipient = ImportedRecipient.objects.select_related("dataset").get(pk=recipient_id)
    cache_key = f"wa-exists:{recipient.phone_normalized}"
    cached = cache.get(cache_key)
    if cached is not None:
        recipient.whatsapp_state = ImportedRecipient.WhatsApp.EXISTS if cached else ImportedRecipient.WhatsApp.NOT_EXISTS
        recipient.whatsapp_checked_at = timezone.now()
        recipient.save(update_fields=["whatsapp_state", "whatsapp_checked_at", "updated_at"])
        return
    recipient.whatsapp_state = ImportedRecipient.WhatsApp.CHECKING
    recipient.save(update_fields=["whatsapp_state", "updated_at"])
    try:
        result = WasenderClient().check_number(recipient.phone_normalized)
        exists = bool(result.data.get("data", {}).get("exists"))
        cache.set(cache_key, exists, settings.WASENDER_CHECK_CACHE_SECONDS)
        recipient.whatsapp_state = ImportedRecipient.WhatsApp.EXISTS if exists else ImportedRecipient.WhatsApp.NOT_EXISTS
        recipient.whatsapp_checked_at = timezone.now()
        recipient.save(update_fields=["whatsapp_state", "whatsapp_checked_at", "updated_at"])
    except RateLimitError as exc:
        raise self.retry(exc=exc, countdown=int(exc.retry_after or 60))
    except WasenderError:
        recipient.whatsapp_state = ImportedRecipient.WhatsApp.ERROR
        recipient.save(update_fields=["whatsapp_state", "updated_at"])


def queue_campaign_entries(campaign):
    selected = campaign.dataset.recipients.filter(selected=True).order_by("original_row_number")
    seen = set()
    entries = []
    for recipient in selected:
        state, reason = CampaignRecipient.State.QUEUED, ""
        rendered, missing = render_message(campaign.body_snapshot, recipient.row_data, campaign.missing_value_policy, campaign.missing_value_fallback)
        if recipient.phone_validation_status not in {"valid", "warning"}:
            state, reason = CampaignRecipient.State.INVALID, recipient.validation_error_message or "Invalid phone"
        elif recipient.suppressed:
            state, reason = CampaignRecipient.State.SKIPPED, "Suppressed recipient"
        elif recipient.whatsapp_state == ImportedRecipient.WhatsApp.NOT_EXISTS and not campaign.allow_unknown:
            state, reason = CampaignRecipient.State.SKIPPED, "Not on WhatsApp"
        elif recipient.whatsapp_state in {ImportedRecipient.WhatsApp.UNKNOWN, ImportedRecipient.WhatsApp.ERROR} and not campaign.allow_unknown:
            state, reason = CampaignRecipient.State.SKIPPED, "WhatsApp status unknown"
        elif rendered is None:
            state, reason = CampaignRecipient.State.SKIPPED, f"Missing values: {', '.join(missing)}"
        elif recipient.phone_normalized in seen and not campaign.allow_duplicates:
            state, reason = CampaignRecipient.State.SKIPPED, "Duplicate number"
        seen.add(recipient.phone_normalized)
        entries.append(CampaignRecipient(campaign=campaign, imported_recipient=recipient, rendered_message=rendered or "", normalized_phone=recipient.phone_normalized, state=state, skip_reason=reason, queued_at=timezone.now()))
    CampaignRecipient.objects.bulk_create(entries, ignore_conflicts=True)
    campaign.total_count = len(entries)
    campaign.selected_recipient_count = len(entries)
    campaign.queued_count = sum(e.state == CampaignRecipient.State.QUEUED for e in entries)
    campaign.skipped_count = sum(e.state in {CampaignRecipient.State.SKIPPED, CampaignRecipient.State.INVALID} for e in entries)
    campaign.save()


@shared_task
def dispatch_campaign(campaign_id):
    campaign = Campaign.objects.get(pk=campaign_id)
    if campaign.status in {Campaign.Status.CANCELLED, Campaign.Status.PAUSED}: return
    if not campaign.campaign_recipients.exists():
        queue_campaign_entries(campaign)
    campaign.status = Campaign.Status.SENDING
    campaign.started_at = campaign.started_at or timezone.now()
    campaign.save(update_fields=["status", "started_at", "updated_at"])
    interval = max(60 if settings.WASENDER_TRIAL_MODE else 0, settings.WASENDER_SEND_INTERVAL_SECONDS)
    for index, entry_id in enumerate(campaign.campaign_recipients.filter(state=CampaignRecipient.State.QUEUED).values_list("id", flat=True)):
        send_campaign_recipient.apply_async(args=[str(entry_id)], countdown=index * interval)


@shared_task(bind=True, max_retries=3)
def send_campaign_recipient(self, entry_id):
    with transaction.atomic():
        entry = CampaignRecipient.objects.select_for_update().select_related("campaign", "campaign__media").get(pk=entry_id)
        campaign = entry.campaign
        if campaign.status in {Campaign.Status.PAUSED, Campaign.Status.CANCELLED} or entry.state != CampaignRecipient.State.QUEUED:
            return
        lock_key = f"wasender-session-send:{campaign.owner_id}"
        interval = max(60 if settings.WASENDER_TRIAL_MODE else 0, settings.WASENDER_SEND_INTERVAL_SECONDS)
        if not cache.add(lock_key, "locked", timeout=max(1, interval)):
            raise self.retry(countdown=max(1, interval), max_retries=None)
        attempt_number = entry.attempts.count() + 1
        payload = {"to": entry.normalized_phone, "text": entry.rendered_message}
        attempt = MessageAttempt.objects.create(campaign_recipient=entry, attempt_number=attempt_number, request_payload=payload)
    started = time.monotonic()
    try:
        media_field, media_url = None, None
        if campaign.media:
            if (not campaign.media.provider_public_url or
                    not campaign.media.provider_url_expires_at or
                    campaign.media.provider_url_expires_at <= timezone.now()):
                upload_media(str(campaign.media.id))
                campaign.media.refresh_from_db()
                if campaign.media.upload_status != UploadedMedia.Status.READY:
                    raise WasenderError(f"Media upload unavailable: {campaign.media.upload_error}")
            media_field = {"image": "imageUrl", "video": "videoUrl", "audio": "audioUrl", "document": "documentUrl"}.get(campaign.media.media_type)
            media_url = campaign.media.provider_public_url
        result = WasenderClient().send_message(entry.normalized_phone, entry.rendered_message, media_field, media_url)
        data = result.data.get("data", {})
        with transaction.atomic():
            entry = CampaignRecipient.objects.select_for_update().get(pk=entry_id)
            entry.provider_message_id = str(data.get("msgId", ""))
            entry.provider_jid = str(data.get("jid", ""))
            entry.provider_status_text = str(data.get("status", "accepted"))
            entry.state = CampaignRecipient.State.ACCEPTED
            entry.last_provider_payload = result.data
            entry.sent_at = timezone.now()
            entry.save()
            attempt.http_status, attempt.provider_response = result.http_status, result.data
            attempt.provider_message_id = entry.provider_message_id
            attempt.duration_ms = int((time.monotonic() - started) * 1000)
            attempt.save()
    except WasenderError as exc:
        attempt.http_status, attempt.error_category, attempt.error_message = exc.status, exc.category, str(exc)
        attempt.provider_response, attempt.duration_ms = exc.payload, int((time.monotonic() - started) * 1000)
        attempt.save()
        transient = exc.category in {"timeout", "connection", "rate_limit"} or (exc.status and exc.status >= 500)
        if transient and self.request.retries < 3:
            entry.retry_count += 1; entry.save(update_fields=["retry_count", "updated_at"])
            raise self.retry(exc=exc, countdown=int(exc.retry_after or (2 ** self.request.retries * 30 + random.randint(0, 10))))
        entry.state, entry.failed_at = CampaignRecipient.State.FAILED, timezone.now()
        entry.save(update_fields=["state", "failed_at", "updated_at"])
    refresh_campaign.delay(str(campaign.id))


@shared_task
def refresh_campaign(campaign_id):
    campaign = Campaign.objects.get(pk=campaign_id)
    counts = dict(campaign.campaign_recipients.values("state").annotate(n=Count("id")).values_list("state", "n"))
    campaign.sent_count = sum(counts.get(s, 0) for s in ["accepted", "pending", "sent", "delivered", "read", "played"])
    campaign.delivered_count = sum(counts.get(s, 0) for s in ["delivered", "read", "played"])
    campaign.read_count = sum(counts.get(s, 0) for s in ["read", "played"])
    campaign.failed_count = counts.get("failed", 0)
    campaign.skipped_count = counts.get("skipped", 0) + counts.get("invalid", 0) + counts.get("cancelled", 0)
    outstanding = counts.get("queued", 0) + counts.get("pending", 0)
    if not outstanding and campaign.status not in {Campaign.Status.CANCELLED, Campaign.Status.PAUSED}:
        campaign.status = Campaign.Status.ERRORS if campaign.failed_count or campaign.skipped_count else Campaign.Status.COMPLETED
        campaign.completed_at = timezone.now()
    campaign.save()


STATUS_MAP = {0: "failed", 1: "pending", 2: "sent", 3: "delivered", 4: "read", 5: "played"}
STATUS_RANK = {"failed": 0, "queued": 0, "accepted": 1, "pending": 1, "sent": 2, "delivered": 3, "read": 4, "played": 5}


@shared_task
def process_webhook(event_id):
    event = WebhookEvent.objects.get(pk=event_id)
    payload = event.payload
    data = payload.get("data", payload)
    message_id = str(data.get("msgId") or data.get("messageId") or "")
    code = data.get("status")
    try: code = int(code)
    except (TypeError, ValueError): code = None
    entry = CampaignRecipient.objects.filter(provider_message_id=message_id).first()
    if entry and code in STATUS_MAP:
        state = STATUS_MAP[code]
        if STATUS_RANK.get(state, 0) >= STATUS_RANK.get(entry.state, 0) or state == "failed":
            entry.state, entry.provider_status_code = state, code
            entry.last_provider_payload = payload
            now = timezone.now()
            if state == "sent": entry.sent_at = entry.sent_at or now
            if state == "delivered": entry.delivered_at = entry.delivered_at or now
            if state in {"read", "played"}: entry.read_at = entry.read_at or now
            if state == "failed": entry.failed_at = entry.failed_at or now
            entry.save()
            refresh_campaign.delay(str(entry.campaign_id))
    event.processing_state, event.processed_at = "processed", timezone.now()
    event.save(update_fields=["processing_state", "processed_at", "updated_at"])


@shared_task
def upload_media(media_id):
    media = UploadedMedia.objects.get(pk=media_id)
    media.upload_status = UploadedMedia.Status.UPLOADING; media.save(update_fields=["upload_status", "updated_at"])
    try:
        with media.original_file.open("rb") as source:
            result = WasenderClient().upload_media(source, media.mime_type)
        media.provider_public_url = result.data.get("publicUrl") or result.data.get("data", {}).get("publicUrl", "")
        media.provider_url_expires_at = timezone.now() + timedelta(hours=23)
        media.upload_status = UploadedMedia.Status.READY
        media.upload_error = ""
    except WasenderError as exc:
        media.upload_status, media.upload_error = UploadedMedia.Status.FAILED, str(exc)
    media.save()


@shared_task
def reconcile_pending_messages():
    cutoff = timezone.now() - timedelta(minutes=15)
    entries = CampaignRecipient.objects.filter(state__in=["accepted", "pending"], updated_at__lt=cutoff).exclude(provider_message_id="")[:50]
    for entry in entries:
        try:
            result = WasenderClient().message_info(entry.provider_message_id)
            data = result.data.get("data", result.data)
            code = int(data.get("status"))
            state = STATUS_MAP.get(code)
            if state and STATUS_RANK.get(state, 0) >= STATUS_RANK.get(entry.state, 0):
                entry.state, entry.provider_status_code = state, code
                entry.last_provider_payload = result.data
                entry.save()
                refresh_campaign.delay(str(entry.campaign_id))
        except (WasenderError, TypeError, ValueError):
            continue
