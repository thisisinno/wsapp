import math
import time
from datetime import timedelta

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from api.models import CampaignRecipient, MessageAttempt
from api.services.campaigns import campaign_interval_seconds, claim_provider_send_slot, extend_provider_send_gate, provider_rate_limited, provider_retry_seconds, recalculate_campaign
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
    payload = provider_payload if isinstance(provider_payload, dict) else {}
    data_present = isinstance(payload.get("data"), dict)
    data = payload.get("data") if data_present else payload
    if not isinstance(data, dict): data = {}
    raw_status = data.get("status")
    raw_code = data.get("ack", data.get("statusCode", data.get("status_code")))
    for value in (raw_status, data.get("messageStatus"), data.get("message_status"),
                  (data.get("receipt") or {}).get("status") if isinstance(data.get("receipt"), dict) else None,
                  (data.get("message") or {}).get("status") if isinstance(data.get("message"), dict) else None):
        if raw_code is None and isinstance(value, (int, float)):
            raw_code = value
        if not raw_status and value is not None:
            raw_status = value
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
    entry.status_sync_error = "" if normalized != "unknown" else "Provider response did not include a recognized acknowledgement."
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


def apply_send_result(entry_id, provider_result, *, attempt_id=None, finished_at=None, duration_ms=0):
    """Atomically persist an accepted provider response before status polling can see it."""
    finished_at = finished_at or timezone.now()
    payload = getattr(provider_result, "data", provider_result)
    data = _provider_data(provider_result)
    message_id = str(data.get("msgId") or data.get("id") or "").strip()
    with transaction.atomic():
        entry = CampaignRecipient.objects.select_for_update().select_related("campaign").get(pk=entry_id)
        entry.state = CampaignRecipient.State.ACCEPTED
        entry.provider_message_id = message_id
        entry.provider_jid = str(data.get("jid") or "")[:255]
        entry.attempt_finished_at = finished_at
        entry.skip_reason = ""
        entry.failed_at = None
        entry.last_provider_payload = safe_provider_payload(payload)
        entry.status_sync_error = "" if message_id else "Provider accepted the request but did not return a message ID."
        entry.next_status_check_at = finished_at if message_id else None
        entry.save(update_fields=["state", "provider_message_id", "provider_jid", "attempt_finished_at", "skip_reason", "failed_at", "last_provider_payload", "status_sync_error", "next_status_check_at", "updated_at"])
        # This second save only observes acknowledgement fields; identity above is already durable.
        if message_id:
            entry, _ = apply_provider_status(entry, payload, checked_at=finished_at)
        if attempt_id:
            attempt = MessageAttempt.objects.select_for_update().get(pk=attempt_id)
            attempt.http_status = getattr(provider_result, "http_status", None)
            attempt.provider_response = safe_provider_payload(payload)
            attempt.provider_message_id = message_id
            attempt.duration_ms = duration_ms
            attempt.save()
        recalculate_campaign(entry.campaign_id, finalize=False)
    return entry


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


def _resend_message_legacy(entry_id, owner_id, data, client=None):
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
        now = timezone.now()
        interval = campaign_interval_seconds(entry.campaign)
        claim = claim_provider_send_slot(requested_interval=interval, campaign_id=entry.campaign_id, recipient_id=entry.id)
        if not claim["claimed"]:
            raise MessageActionError("The provider queue requires a 5-second interval.", status=429,
                                     data={"wait_seconds": claim["wait_seconds"], "next_allowed_at": claim["next_allowed_at"].isoformat()})
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
                result = provider_client.send_message(
                    entry.normalized_phone, entry.rendered_message, media_field, media_url,
                    media.original_filename if media_field == "documentUrl" else None,
                )
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
            attempt.http_status = result.http_status if isinstance(result.http_status, int) else None
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


def resend_message(entry_id, owner_id, data, client=None):
    """Resend using the same global gate; the provider request is outside DB locks."""
    text = _validate_text(data.get("text"))
    phone_result = normalize_tanzania_phone(data.get("phone"))
    if phone_result.status not in {"valid", "warning"}:
        raise MessageActionError(phone_result.message or "Enter a valid Tanzania phone number.", fields={"phone": [phone_result.message]})
    with transaction.atomic():
        entry = CampaignRecipient.objects.select_for_update().select_related("campaign").get(pk=entry_id, campaign__owner_id=owner_id)
        if entry.state != CampaignRecipient.State.FAILED or entry.provider_deleted_at:
            raise MessageActionError("Only active failed messages can be resent.", status=409)
        claim = claim_provider_send_slot(requested_interval=campaign_interval_seconds(entry.campaign), campaign_id=entry.campaign_id, recipient_id=entry.id)
        if not claim["claimed"]:
            raise MessageActionError("The provider queue requires a 5-second interval.", status=429, data={"wait_seconds": claim["wait_seconds"]})
        native_resend = phone_result.normalized == entry.normalized_phone and text == entry.rendered_message and bool(entry.provider_message_id)
        entry.normalized_phone, entry.rendered_message = phone_result.normalized, text
        entry.state, entry.attempt_started_at, entry.provider_action_error = CampaignRecipient.State.PROCESSING, timezone.now(), ""
        entry.save()
        attempt = MessageAttempt.objects.create(campaign_recipient=entry, attempt_number=entry.attempts.count() + 1, request_payload={"to": text and entry.normalized_phone, "text": text})
        provider_id, phone, message = entry.provider_message_id, entry.normalized_phone, entry.rendered_message
    started = time.monotonic()
    try:
        result = (client or WasenderClient()).resend_message(provider_id) if native_resend else (client or WasenderClient()).send_message(phone, message)
        entry = apply_send_result(entry.id, result, attempt_id=attempt.id, finished_at=timezone.now(), duration_ms=int((time.monotonic() - started) * 1000))
        entry.retry_count += 1
        entry.save(update_fields=["retry_count", "updated_at"])
        return entry
    except WasenderError as exc:
        if provider_rate_limited(exc):
            extend_provider_send_gate(provider_retry_seconds(exc, campaign_interval_seconds(entry.campaign)))
        with transaction.atomic():
            entry = CampaignRecipient.objects.select_for_update().get(pk=entry_id)
            entry.state, entry.failed_at, entry.attempt_finished_at = CampaignRecipient.State.FAILED, timezone.now(), timezone.now()
            entry.skip_reason, entry.provider_action_error = safe_provider_text(exc)[:255], safe_provider_text(exc)
            entry.last_provider_payload = safe_provider_payload(exc.payload)
            entry.save()
            attempt = MessageAttempt.objects.select_for_update().get(pk=attempt.id)
            attempt.http_status, attempt.provider_response, attempt.error_category, attempt.error_message = exc.status, entry.last_provider_payload, exc.category, entry.skip_reason
            attempt.duration_ms = int((time.monotonic() - started) * 1000)
            attempt.save()
            recalculate_campaign(entry.campaign_id, finalize=False)
        raise MessageActionError(entry.skip_reason, status=exc.status if exc.status and exc.status < 500 else 502)


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
        "status_sync_error": entry.status_sync_error,
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
        "pending": campaign.pending_count,
        "sent": campaign.sent_count,
        "delivered": campaign.delivered_count,
        "read": campaign.read_count,
        "failed": campaign.failed_count,
    }
