import math
import time
from datetime import timedelta

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from api.models import CampaignRecipient, MessageAttempt
from api.services.campaigns import campaign_interval_seconds, recalculate_campaign
from api.services.media import ensure_media_ready
from api.services.phones import normalize_tanzania_phone
from api.services.wasender import (
    ACK_LEVELS,
    WasenderClient,
    WasenderError,
    normalize_message_status,
    safe_provider_payload,
    safe_provider_text,
)

MAX_MESSAGE_LENGTH = 4096
DELIVERED_STATES = {"delivered", "read", "played"}
STATUS_SYNC_ELIGIBLE_STATES = {"accepted", "pending", "sent", "delivered"}
STATUS_SYNC_FINAL_STATES = {"read", "played", "failed"}
STATUS_SYNC_PENDING_SECONDS = 5
STATUS_SYNC_SENT_SECONDS = 5
STATUS_SYNC_DELIVERED_SECONDS = 10
STATUS_SYNC_ERROR_BACKOFF_SECONDS = 30
STATUS_SYNC_BATCH_SIZE = 5


class MessageActionError(Exception):
    def __init__(self, message, status=400, fields=None, data=None):
        super().__init__(message)
        self.status = status
        self.fields = fields or {}
        self.data = data or {}


def _provider_data(result):
    data = result.data.get("data", result.data)
    return data if isinstance(data, dict) else {}


def _provider_failure(entry, exc):
    message = safe_provider_text(exc)
    entry.provider_action_error = message
    entry.save(update_fields=["provider_action_error", "updated_at"])
    raise MessageActionError(
        message,
        status=exc.status if exc.status and exc.status < 500 else 502,
    )


def _validate_text(value):
    text = str(value or "").strip()
    if not text:
        raise MessageActionError("Message text is required.", fields={"text": ["Required."]})
    if len(text) > MAX_MESSAGE_LENGTH:
        raise MessageActionError(
            f"Message must be {MAX_MESSAGE_LENGTH} characters or fewer.",
            fields={"text": ["Message is too long."]},
        )
    return text


def provider_status_values(provider_payload):
    """Read acknowledgement variants without guessing beyond documented values."""
    data = provider_payload.get("data", provider_payload) if isinstance(provider_payload, dict) else {}
    data = data if isinstance(data, dict) else {}
    raw_status = data.get("status")
    raw_code = data.get("ack")
    if raw_code is None:
        raw_code = data.get("statusCode", data.get("status_code"))
    if raw_code is None and isinstance(raw_status, (int, float)):
        raw_code = raw_status
    raw_text = data.get("statusText") or (raw_status if not isinstance(raw_status, (int, float)) else "") or ""
    return raw_code, raw_text


def status_sync_delay(state):
    return STATUS_SYNC_DELIVERED_SECONDS if state == "delivered" else (
        STATUS_SYNC_SENT_SECONDS if state == "sent" else STATUS_SYNC_PENDING_SECONDS
    )


def apply_provider_status(entry, provider_payload, checked_at=None):
    """Apply one provider observation monotonically. Caller holds the row lock."""
    now = checked_at or timezone.now()
    raw_code, raw_text = provider_status_values(provider_payload)
    normalized = normalize_message_status(raw_code, raw_text)
    old_state = entry.state
    old_visible = (entry.state, entry.sent_at, entry.delivered_at, entry.read_at, entry.failed_at,
                   entry.provider_status_code, entry.provider_status_text)
    old_level = ACK_LEVELS.get(old_state, -1)
    new_level = ACK_LEVELS.get(normalized, -1)
    try:
        entry.provider_status_code = int(raw_code) if raw_code is not None else None
    except (TypeError, ValueError):
        entry.provider_status_code = None
    entry.provider_status_text = str(raw_text or raw_code or "")[:100]
    entry.provider_status_checked_at = now
    entry.last_provider_payload = safe_provider_payload(provider_payload)
    entry.status_sync_failure_count = 0
    entry.status_sync_error = ""
    # Failures are meaningful only before a confirmed delivery acknowledgement.
    if normalized == "failed":
        if old_state not in DELIVERED_STATES:
            entry.state = "failed"
    elif new_level > old_level:
        entry.state = normalized
    if ACK_LEVELS.get(entry.state, -1) >= ACK_LEVELS["sent"] and not entry.sent_at:
        entry.sent_at = now
    if entry.state in DELIVERED_STATES and not entry.delivered_at:
        entry.delivered_at = now
    if entry.state in {"read", "played"} and not entry.read_at:
        entry.read_at = now
    if entry.state == "failed" and not entry.failed_at:
        entry.failed_at = now
    entry.next_status_check_at = (
        now + timedelta(seconds=status_sync_delay(entry.state))
        if entry.state in STATUS_SYNC_ELIGIBLE_STATES and not entry.provider_deleted_at else None
    )
    changed = old_visible != (entry.state, entry.sent_at, entry.delivered_at, entry.read_at, entry.failed_at,
                              entry.provider_status_code, entry.provider_status_text)
    # Include state/timestamps for callers that set acceptance immediately before
    # applying the provider acknowledgement, but retain updated_at for genuine
    # visible changes only.
    fields = ["state", "sent_at", "delivered_at", "read_at", "failed_at",
              "provider_status_code", "provider_status_text", "provider_status_checked_at",
              "last_provider_payload", "status_sync_failure_count", "status_sync_error", "next_status_check_at"]
    if changed:
        fields.append("updated_at")
    entry.save(update_fields=fields)
    if entry.state != old_state:
        recalculate_campaign(entry.campaign_id, finalize=False)
    return entry, changed


def record_status_sync_error(entry, exc, checked_at=None):
    now = checked_at or timezone.now()
    entry.status_sync_failure_count += 1
    entry.status_sync_error = safe_provider_text(exc)[:255]
    retry_after = getattr(exc, "retry_after", None)
    try:
        delay = max(STATUS_SYNC_ERROR_BACKOFF_SECONDS, int(retry_after or 0))
    except (TypeError, ValueError):
        delay = STATUS_SYNC_ERROR_BACKOFF_SECONDS
    delay = min(60, delay * max(1, min(entry.status_sync_failure_count, 2)))
    entry.provider_status_checked_at = now
    entry.next_status_check_at = now + timedelta(seconds=delay)
    entry.save(update_fields=["status_sync_failure_count", "status_sync_error", "provider_status_checked_at", "next_status_check_at"])


def refresh_status(entry, client=None):
    if not entry.provider_message_id:
        raise MessageActionError("This message has no provider message ID.")
    if entry.provider_deleted_at:
        raise MessageActionError("A deleted provider message cannot be refreshed.")
    try:
        result = (client or WasenderClient()).message_info(entry.provider_message_id)
    except WasenderError as exc:
        record_status_sync_error(entry, exc)
        raise MessageActionError(safe_provider_text(exc), status=exc.status if exc.status and exc.status < 500 else 502)
    with transaction.atomic():
        locked = CampaignRecipient.objects.select_for_update().select_related("campaign").get(pk=entry.pk)
        locked, _ = apply_provider_status(locked, result.data)
    return locked


def update_message(entry, data, client=None):
    text = _validate_text(data.get("text"))
    if entry.provider_deleted_at:
        raise MessageActionError("A deleted provider message cannot be updated.")
    if entry.provider_message_id:
        try:
            result = (client or WasenderClient()).edit_message(entry.provider_message_id, text)
        except WasenderError as exc:
            _provider_failure(entry, exc)
        entry.rendered_message = text
        entry.provider_edited_at = timezone.now()
        entry.last_provider_payload = safe_provider_payload(result.data)
        entry.provider_action_error = ""
        entry.save()
        return entry
    if entry.state != CampaignRecipient.State.FAILED:
        raise MessageActionError("Only a failed message without a provider ID can be corrected locally.")
    phone_result = normalize_tanzania_phone(data.get("phone", entry.normalized_phone))
    if phone_result.status not in {"valid", "warning"}:
        raise MessageActionError(
            phone_result.message or "Enter a valid Tanzania phone number.",
            fields={"phone": [phone_result.message]},
        )
    entry.normalized_phone = phone_result.normalized
    entry.rendered_message = text
    entry.provider_action_error = ""
    entry.save()
    if data.get("update_imported_recipient") is True:
        source = entry.imported_recipient
        source.phone_original = str(data.get("phone", ""))
        source.phone_normalized = phone_result.normalized
        source.phone_validation_status = phone_result.status
        source.validation_error_code = phone_result.code
        source.validation_error_message = phone_result.message
        source.auto_corrected = phone_result.auto_corrected
        source.row_data[source.phone_source_column] = str(data.get("phone", ""))
        source.save()
    return entry


def delete_message(entry, client=None):
    if entry.provider_deleted_at:
        raise MessageActionError("This message was already deleted from WhatsApp.", status=409)
    if not entry.provider_message_id:
        raise MessageActionError("This message has no provider message ID.")
    try:
        result = (client or WasenderClient()).delete_message(entry.provider_message_id)
    except WasenderError as exc:
        _provider_failure(entry, exc)
    entry.provider_deleted_at = timezone.now()
    entry.last_provider_payload = safe_provider_payload(result.data)
    entry.provider_action_error = ""
    entry.save()
    return entry


def resend_message(entry_id, owner_id, data, client=None):
    text = _validate_text(data.get("text"))
    phone_result = normalize_tanzania_phone(data.get("phone"))
    if phone_result.status not in {"valid", "warning"}:
        raise MessageActionError(
            phone_result.message or "Enter a valid Tanzania phone number.",
            fields={"phone": [phone_result.message]},
        )
    provider_client = client or WasenderClient()
    started = time.monotonic()
    failure = None
    with transaction.atomic():
        entry = (
            CampaignRecipient.objects.select_for_update()
            .select_related("campaign", "imported_recipient")
            .get(pk=entry_id, campaign__owner_id=owner_id)
        )
        if entry.state != CampaignRecipient.State.FAILED:
            raise MessageActionError("Only failed messages can be resent.", status=409)
        if entry.provider_deleted_at:
            raise MessageActionError("A deleted provider message cannot be resent.", status=409)
        latest = (
            MessageAttempt.objects.filter(
                campaign_recipient__campaign__owner_id=owner_id,
                campaign_recipient__attempt_started_at__isnull=False,
            ).order_by("-campaign_recipient__attempt_started_at")
            .first()
        )
        now = timezone.now()
        interval = campaign_interval_seconds(entry.campaign)
        if latest and interval:
            next_allowed = latest.campaign_recipient.attempt_started_at + timedelta(seconds=interval)
            if next_allowed > now:
                wait = max(1, math.ceil((next_allowed - now).total_seconds()))
                raise MessageActionError(
                    "The campaign send interval is still active.",
                    status=429,
                    data={"wait_seconds": wait},
                )
        unchanged = phone_result.normalized == entry.normalized_phone and text == entry.rendered_message
        entry.normalized_phone = phone_result.normalized
        entry.rendered_message = text
        entry.state = CampaignRecipient.State.PROCESSING
        entry.attempt_started_at = now
        entry.provider_action_error = ""
        entry.save()
        attempt = MessageAttempt.objects.create(
            campaign_recipient=entry,
            attempt_number=entry.attempts.count() + 1,
            request_payload={"to": entry.normalized_phone, "text": entry.rendered_message},
        )
        try:
            if unchanged and entry.provider_message_id:
                result = provider_client.resend_message(entry.provider_message_id)
            else:
                media_field = media_url = None
                if entry.campaign.media_id:
                    media = ensure_media_ready(entry.campaign.media)
                    media_field = {"image": "imageUrl", "video": "videoUrl", "audio": "audioUrl", "document": "documentUrl"}.get(media.media_type)
                    media_url = media.provider_public_url
                    attempt.request_payload = {"to": entry.normalized_phone, "text": entry.rendered_message, "media_type": media.media_type, "media_url": media_url, "original_filename": media.original_filename}
                    attempt.save(update_fields=["request_payload", "updated_at"])
                result = provider_client.send_message(entry.normalized_phone, entry.rendered_message, media_field, media_url)
            provider_data = _provider_data(result)
            status_text = provider_data.get("status", "pending")
            normalized = normalize_message_status(
                provider_data.get("ack", provider_data.get("statusCode")),
                status_text,
            )
            entry.state = normalized if normalized in {"sent", "delivered", "read", "played"} else CampaignRecipient.State.PENDING
            entry.provider_message_id = str(
                provider_data.get("msgId") or provider_data.get("id") or entry.provider_message_id
            )
            entry.provider_jid = str(provider_data.get("jid") or entry.provider_jid)
            entry.provider_status_text = str(status_text)[:100]
            entry.last_provider_payload = safe_provider_payload(result.data)
            entry.retry_count += 1
            entry.skip_reason = ""
            entry.failed_at = None
            entry.provider_action_error = ""
            entry.attempt_finished_at = timezone.now()
            if ACK_LEVELS.get(entry.state, -1) >= ACK_LEVELS["sent"] and not entry.sent_at:
                entry.sent_at = timezone.now()
            entry.save()
            attempt.http_status = result.http_status
            attempt.provider_response = entry.last_provider_payload
            attempt.provider_message_id = entry.provider_message_id
            attempt.duration_ms = int((time.monotonic() - started) * 1000)
            attempt.save()
        except WasenderError as exc:
            safe_error = safe_provider_text(exc)
            entry.state = CampaignRecipient.State.FAILED
            entry.failed_at = timezone.now()
            entry.attempt_finished_at = timezone.now()
            entry.skip_reason = safe_error[:255]
            entry.provider_action_error = safe_error
            entry.last_provider_payload = safe_provider_payload(exc.payload)
            entry.save()
            attempt.http_status = exc.status
            attempt.provider_response = entry.last_provider_payload
            attempt.error_category = exc.category
            attempt.error_message = safe_error
            attempt.duration_ms = int((time.monotonic() - started) * 1000)
            attempt.save()
            recalculate_campaign(entry.campaign_id, finalize=False)
            failure = MessageActionError(
                safe_error,
                status=exc.status if exc.status and exc.status < 500 else 502,
            )
        if failure is None:
            recalculate_campaign(entry.campaign_id, finalize=False)
    if failure:
        raise failure
    return entry


def message_queryset(owner):
    return (
        CampaignRecipient.objects.filter(campaign__owner=owner)
        .select_related("campaign", "imported_recipient")
        .annotate(attempt_count=Count("attempts", distinct=True))
    )


def serialize_row(entry, serial_number=None):
    pending_states = {"queued", "processing", "accepted", "pending"}
    state = (
        "deleted"
        if entry.provider_deleted_at
        else "pending"
        if entry.state in pending_states
        else entry.state
        if entry.state in ACK_LEVELS
        else "unknown"
    )
    labels = {
        "pending": "Pending",
        "accepted": "Pending",
        "sent": "Sent",
        "delivered": "Delivered",
        "read": "Read",
        "played": "Played",
        "failed": "Failed",
        "deleted": "Deleted",
        "unknown": "Unknown",
    }
    attempt_count = getattr(entry, "attempt_count", None)
    if attempt_count is None:
        attempt_count = entry.attempts.count()

    def stamp(value):
        return value.isoformat() if value else None

    return {
        "id": str(entry.id),
        "serial_number": serial_number,
        "campaign_id": str(entry.campaign_id),
        "campaign_name": entry.campaign.name,
        "phone": entry.normalized_phone,
        "message": entry.rendered_message,
        "provider_message_id": entry.provider_message_id,
        "state": state,
        "state_label": labels.get(state, "Unknown"),
        "status_icon": {"pending": "pending", "sent": "sent", "delivered": "delivered", "read": "read", "played": "played", "failed": "failed", "deleted": "deleted"}.get(state, "unknown"),
        "delivery_explanation": "Delivered" if entry.state in DELIVERED_STATES else "Not delivered yet",
        "is_delivered": entry.state in DELIVERED_STATES,
        "is_deleted": bool(entry.provider_deleted_at),
        "provider_status_code": entry.provider_status_code,
        "provider_status_text": entry.provider_status_text,
        "provider_status_checked_at": stamp(entry.provider_status_checked_at),
        "sent_at": stamp(entry.sent_at),
        "delivered_at": stamp(entry.delivered_at),
        "read_at": stamp(entry.read_at),
        "updated_at": stamp(entry.updated_at),
        "failed_at": stamp(entry.failed_at),
        "edited_at": stamp(entry.provider_edited_at),
        "deleted_at": stamp(entry.provider_deleted_at),
        "retry_count": entry.retry_count,
        "attempt_count": attempt_count,
        "failure_reason": entry.skip_reason or entry.provider_action_error,
        "status_sync_eligible": bool(entry.provider_message_id and not entry.provider_deleted_at and entry.state in STATUS_SYNC_ELIGIBLE_STATES),
        "next_status_check_at": stamp(entry.next_status_check_at),
        "can_update": not entry.provider_deleted_at and bool(
            entry.provider_message_id or entry.state == CampaignRecipient.State.FAILED
        ),
        "can_delete": bool(entry.provider_message_id and not entry.provider_deleted_at),
        "can_refresh_status": bool(entry.provider_message_id and not entry.provider_deleted_at),
        "can_resend": entry.state == CampaignRecipient.State.FAILED and not entry.provider_deleted_at,
    }


def campaign_counts(campaign):
    campaign.refresh_from_db()
    return {
        "sent": campaign.sent_count,
        "delivered": campaign.delivered_count,
        "read": campaign.read_count,
        "failed": campaign.failed_count,
    }
