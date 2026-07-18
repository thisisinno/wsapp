import logging
import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from api.models import Campaign, CampaignRecipient, ImportedRecipient, MessageAttempt
from api.services.media import ensure_media_ready
from api.services.templates import render_message
from api.services.wasender import WasenderClient, WasenderError, safe_provider_payload, safe_provider_text

logger = logging.getLogger(__name__)


ATTEMPTED_STATES = {
    CampaignRecipient.State.ACCEPTED,
    CampaignRecipient.State.PENDING,
    CampaignRecipient.State.SENT,
    CampaignRecipient.State.DELIVERED,
    CampaignRecipient.State.READ,
    CampaignRecipient.State.PLAYED,
    CampaignRecipient.State.FAILED,
}
SENDABLE_STATES = ATTEMPTED_STATES | {
    CampaignRecipient.State.QUEUED,
    CampaignRecipient.State.PROCESSING,
}


def normalize_send_interval(value, default=1):
    """Clamp configuration without changing valid user choices, including zero."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(settings.WASENDER_MIN_SEND_INTERVAL_SECONDS, min(value, settings.WASENDER_MAX_SEND_INTERVAL_SECONDS))


def campaign_interval_seconds(campaign):
    return normalize_send_interval(campaign.send_interval_seconds, settings.WASENDER_SEND_INTERVAL_SECONDS)


def provider_rate_limited(exc):
    """Recognise protection responses even when a provider omits HTTP 429."""
    message = safe_provider_text(exc).lower()
    payload = getattr(exc, "payload", {}) or {}
    category = str(getattr(exc, "category", "") or payload.get("category", "")).lower()
    return (
        getattr(exc, "status", None) == 429
        or "rate" in category and "limit" in category
        or ("rate" in message and ("limit" in message or "protect" in message))
        or "account protection enabled" in message
    )


def provider_retry_seconds(exc, campaign_interval=1):
    payload = getattr(exc, "payload", {}) or {}
    values = [getattr(exc, "retry_after", None)] + [payload.get(key) for key in ("retry_after", "retryAfter", "wait_seconds")]
    message = safe_provider_text(exc).lower()
    import re
    match = re.search(r"(?:wait\s+|every\s+)(\d+)\s*seconds?", message)
    if match:
        values.append(match.group(1))
    match = re.search(r"every\s+\d+\s+message(?:s)?\s+(?:every\s+)?(\d+)\s*seconds?", message)
    if match:
        values.append(match.group(1))
    for value in values:
        try:
            retry = int(value)
        except (TypeError, ValueError):
            continue
        if retry >= 0:
            return retry
    return max(campaign_interval, 1)


def recalculate_campaign(campaign_id, finalize=True):
    campaign = Campaign.objects.get(pk=campaign_id)
    counts = dict(
        campaign.campaign_recipients.values("state")
        .annotate(n=Count("id"))
        .values_list("state", "n")
    )
    campaign.total_count = sum(counts.values())
    campaign.queued_count = counts.get("queued", 0)
    campaign.sent_count = sum(
        counts.get(state, 0)
        for state in ("accepted", "pending", "sent", "delivered", "read", "played")
    )
    campaign.delivered_count = sum(
        counts.get(state, 0) for state in ("delivered", "read", "played")
    )
    campaign.read_count = counts.get("read", 0) + counts.get("played", 0)
    campaign.failed_count = counts.get("failed", 0)
    campaign.skipped_count = counts.get("skipped", 0)
    outstanding = counts.get("queued", 0) + counts.get("processing", 0)
    if (
        finalize
        and not outstanding
        and campaign.status
        not in {Campaign.Status.CANCELLED, Campaign.Status.PAUSED, Campaign.Status.CHECKING}
    ):
        campaign.status = (
            Campaign.Status.ERRORS
            if campaign.failed_count
            else Campaign.Status.COMPLETED
        )
        campaign.completed_at = timezone.now()
    campaign.last_progress_at = timezone.now()
    campaign.queue_error = ""
    campaign.dispatch_task_id = ""
    campaign.save()
    return counts


def queue_campaign_entries(campaign):
    selected = campaign.dataset.recipients.filter(selected=True).order_by(
        "original_row_number"
    )
    seen = set()
    existing = {
        item.imported_recipient_id: item
        for item in campaign.campaign_recipients.all()
    }
    entries = []
    now = timezone.now()
    for sequence, recipient in enumerate(selected, 1):
        if recipient.id in existing:
            entry = existing[recipient.id]
            if entry.sequence_number is None:
                entry.sequence_number = sequence
                entry.save(update_fields=["sequence_number", "updated_at"])
            if recipient.phone_normalized:
                seen.add(recipient.phone_normalized)
            continue
        state = CampaignRecipient.State.QUEUED
        reason = ""
        rendered, missing = render_message(
            campaign.body_snapshot,
            recipient.row_data,
            campaign.missing_value_policy,
            campaign.missing_value_fallback,
        )
        if recipient.phone_validation_status not in {"valid", "warning"}:
            state = CampaignRecipient.State.INVALID
            reason = recipient.validation_error_message or "Invalid phone"
        elif recipient.suppressed:
            state = CampaignRecipient.State.SKIPPED
            reason = "Suppressed recipient"
        elif recipient.whatsapp_state == ImportedRecipient.WhatsApp.NOT_EXISTS:
            state = CampaignRecipient.State.SKIPPED
            reason = "Number is not registered on WhatsApp."
        elif recipient.whatsapp_state in {
            ImportedRecipient.WhatsApp.UNKNOWN,
            ImportedRecipient.WhatsApp.CHECKING,
            ImportedRecipient.WhatsApp.ERROR,
        } and not campaign.allow_unknown:
            state = CampaignRecipient.State.SKIPPED
            reason = "WhatsApp registration has not been confirmed."
        elif rendered is None:
            state = CampaignRecipient.State.SKIPPED
            reason = f"Missing values: {', '.join(missing)}"
        elif recipient.phone_normalized in seen and not campaign.allow_duplicates:
            state = CampaignRecipient.State.SKIPPED
            reason = "Duplicate number"
        if recipient.phone_normalized:
            seen.add(recipient.phone_normalized)
        entries.append(
            CampaignRecipient(
                campaign=campaign,
                imported_recipient=recipient,
                rendered_message=rendered or "",
                normalized_phone=recipient.phone_normalized,
                state=state,
                skip_reason=reason,
                queued_at=now,
                scheduled_for=now if state == CampaignRecipient.State.QUEUED else None,
                sequence_number=sequence,
            )
        )
    CampaignRecipient.objects.bulk_create(entries, ignore_conflicts=True)
    campaign.selected_recipient_count = selected.count()
    campaign.save(update_fields=["selected_recipient_count", "updated_at"])
    counts = recalculate_campaign(campaign.id, finalize=False)
    return {
        "total": sum(counts.values()),
        "queued": counts.get("queued", 0),
        "skipped": counts.get("skipped", 0),
        "invalid": counts.get("invalid", 0),
    }


def start_campaign(campaign):
    with transaction.atomic():
        campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)
        queue_campaign_entries(campaign)
        if not campaign.campaign_recipients.filter(state="queued").exists():
            recalculate_campaign(campaign.id)
            return campaign
        campaign.run_token = uuid.uuid4().hex
        now = timezone.now()
        campaign.status = Campaign.Status.SENDING
        campaign.started_at = campaign.started_at or now
        campaign.completed_at = None
        campaign.queue_error = ""
        campaign.last_progress_at = now
        first = campaign.campaign_recipients.filter(state="queued").order_by(
            "sequence_number"
        ).first()
        if first:
            first.scheduled_for = now
            first.save(update_fields=["scheduled_for", "updated_at"])
        campaign.save()
    return Campaign.objects.get(pk=campaign.pk)


def resume_campaign(campaign):
    with transaction.atomic():
        campaign = Campaign.objects.select_for_update().get(pk=campaign.pk)
        if campaign.campaign_recipients.filter(state="queued").exists():
            campaign.run_token = uuid.uuid4().hex
            campaign.status = Campaign.Status.SENDING
            campaign.completed_at = None
            campaign.save(
                update_fields=["run_token", "status", "completed_at", "updated_at"]
            )
    return Campaign.objects.get(pk=campaign.pk)


def check_recipient(recipient, client=None):
    now = timezone.now()
    fresh_after = now - timedelta(seconds=settings.WASENDER_CHECK_CACHE_SECONDS)
    if (
        recipient.whatsapp_checked_at
        and recipient.whatsapp_checked_at >= fresh_after
        and recipient.whatsapp_state
        in {ImportedRecipient.WhatsApp.EXISTS, ImportedRecipient.WhatsApp.NOT_EXISTS}
    ):
        return recipient.whatsapp_state
    try:
        result = (client or WasenderClient()).check_number(recipient.phone_normalized)
        data = result.data.get("data", result.data)
        exists = bool(data.get("exists"))
        recipient.whatsapp_state = (
            ImportedRecipient.WhatsApp.EXISTS
            if exists
            else ImportedRecipient.WhatsApp.NOT_EXISTS
        )
    except WasenderError:
        recipient.whatsapp_state = ImportedRecipient.WhatsApp.ERROR
    recipient.whatsapp_checked_at = now
    recipient.save(
        update_fields=["whatsapp_state", "whatsapp_checked_at", "updated_at"]
    )
    return recipient.whatsapp_state


def recover_stale_processing(campaign_id):
    cutoff = timezone.now() - timedelta(
        seconds=settings.WASENDER_CONNECT_TIMEOUT
        + settings.WASENDER_READ_TIMEOUT
        + 15
    )
    stale = CampaignRecipient.objects.filter(
        campaign_id=campaign_id,
        state=CampaignRecipient.State.PROCESSING,
        attempt_started_at__lt=cutoff,
    )
    now = timezone.now()
    for entry in stale.select_for_update():
        entry.state = CampaignRecipient.State.FAILED
        entry.failed_at = now
        entry.attempt_finished_at = now
        entry.skip_reason = "The previous send did not finish. It was marked failed safely."
        entry.save(update_fields=[
            "state", "failed_at", "attempt_finished_at", "skip_reason", "updated_at",
        ])
        entry.attempts.filter(error_message="").update(
            error_category="stale_processing",
            error_message=entry.skip_reason,
        )
    return stale.count()


_safe_provider_payload = safe_provider_payload


def send_next(campaign_id, owner_id, run_token):
    now = timezone.now()
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=owner_id)
        campaign = (
            Campaign.objects.select_for_update()
            .select_related("media")
            .get(pk=campaign_id, owner_id=owner_id)
        )
        if campaign.run_token != run_token:
            return {"conflict": True}
        if campaign.status in {
            Campaign.Status.PAUSED,
            Campaign.Status.CANCELLED,
            Campaign.Status.COMPLETED,
            Campaign.Status.ERRORS,
        }:
            return {"inactive": True}
        recover_stale_processing(campaign.id)
        busy = CampaignRecipient.objects.filter(
            campaign__owner_id=owner_id, state=CampaignRecipient.State.PROCESSING
        ).exists()
        if busy:
            return {"busy": True, "retry_after": 2}
        latest = (
            MessageAttempt.objects.filter(
                campaign_recipient__campaign__owner_id=owner_id,
                campaign_recipient__attempt_started_at__isnull=False,
            )
            .order_by("-campaign_recipient__attempt_started_at")
            .first()
        )
        interval = campaign_interval_seconds(campaign)
        if latest and interval:
            next_allowed = latest.campaign_recipient.attempt_started_at + timedelta(seconds=interval)
            if next_allowed > now:
                return {
                    "wait_seconds": max(
                        1, int((next_allowed - now).total_seconds() + 0.999)
                    )
                }
        entry = (
            campaign.campaign_recipients.select_for_update()
            .filter(state=CampaignRecipient.State.QUEUED)
            .order_by("sequence_number", "created_at")
            .first()
        )
        if not entry:
            recalculate_campaign(campaign.id)
            return {"finished": True}
        entry.state = CampaignRecipient.State.PROCESSING
        entry.attempt_started_at = now
        entry.attempt_finished_at = None
        entry.scheduled_for = None
        entry.save()
        payload = {"to": entry.normalized_phone, "text": entry.rendered_message}
        attempt = MessageAttempt.objects.create(
            campaign_recipient=entry,
            attempt_number=entry.attempts.count() + 1,
            request_payload=payload,
        )
        sequence = entry.sequence_number

    started = time.monotonic()
    result_name = "failed"
    try:
        media_field = media_url = None
        if campaign.media:
            media = ensure_media_ready(campaign.media)
            media_field = {
                "image": "imageUrl",
                "video": "videoUrl",
                "audio": "audioUrl",
                "document": "documentUrl",
            }.get(media.media_type)
            media_url = media.provider_public_url
        provider = WasenderClient().send_message(
            entry.normalized_phone,
            entry.rendered_message,
            media_field,
            media_url,
        )
        data = provider.data.get("data", {})
        safe_payload = _safe_provider_payload(provider.data)
        with transaction.atomic():
            locked = CampaignRecipient.objects.select_for_update().get(pk=entry.pk)
            locked.state = CampaignRecipient.State.ACCEPTED
            locked.provider_message_id = str(data.get("msgId") or data.get("id") or "")
            locked.provider_jid = str(data.get("jid", ""))
            locked.attempt_finished_at = timezone.now()
            locked.skip_reason = ""
            # A successful HTTP request is only acceptance.  Apply any stronger
            # acknowledgement actually returned by the provider.
            from api.services.message_logs import apply_provider_status
            apply_provider_status(locked, provider.data, checked_at=timezone.now())
            attempt.http_status = provider.http_status
            attempt.provider_response = safe_payload
            attempt.provider_message_id = locked.provider_message_id
            attempt.duration_ms = int((time.monotonic() - started) * 1000)
            attempt.save()
        result_name = "accepted"
    except WasenderError as exc:
        with transaction.atomic():
            locked = CampaignRecipient.objects.select_for_update().get(pk=entry.pk)
            is_rate_limited = provider_rate_limited(exc)
            retry_seconds = provider_retry_seconds(exc, campaign_interval_seconds(campaign)) if is_rate_limited else 0
            locked.state = CampaignRecipient.State.QUEUED if is_rate_limited else CampaignRecipient.State.FAILED
            locked.failed_at = None if is_rate_limited else timezone.now()
            locked.attempt_finished_at = timezone.now()
            locked.scheduled_for = timezone.now() + timedelta(seconds=retry_seconds) if is_rate_limited else None
            locked.skip_reason = f"The provider requested a {retry_seconds}-second pause." if is_rate_limited else safe_provider_text(exc)[:255]
            locked.save()
            attempt.http_status = exc.status
            attempt.provider_response = _safe_provider_payload(exc.payload)
            attempt.error_category = "rate_limited" if is_rate_limited else exc.category
            attempt.error_message = locked.skip_reason
            attempt.duration_ms = int((time.monotonic() - started) * 1000)
            attempt.save()
            if is_rate_limited:
                result_name = "rate_limited"
    except Exception:
        # Log a traceback-shaped safe diagnostic without interpolating the
        # original exception, which may contain a provider response.
        try:
            raise RuntimeError("Unexpected provider processing error.") from None
        except RuntimeError:
            logger.exception(
                "Unexpected provider processing failure for campaign recipient %s",
                entry.pk,
            )
        with transaction.atomic():
            locked = CampaignRecipient.objects.select_for_update().get(pk=entry.pk)
            locked.state = CampaignRecipient.State.FAILED
            locked.failed_at = timezone.now()
            locked.attempt_finished_at = timezone.now()
            locked.skip_reason = "An unexpected server error prevented this send."
            locked.save()
            attempt.error_category = "unexpected"
            attempt.error_message = locked.skip_reason
            attempt.duration_ms = int((time.monotonic() - started) * 1000)
            attempt.save()
    finally:
        with transaction.atomic():
            campaign = Campaign.objects.select_for_update().get(pk=campaign_id)
            recalculate_campaign(campaign.id, finalize=False)
            next_entry = campaign.campaign_recipients.filter(state="queued").order_by(
                "sequence_number", "created_at"
            ).first()
            wait_seconds = campaign_interval_seconds(campaign) if next_entry else 0
            if next_entry:
                # Keep a provider-requested delay; never shorten it.
                scheduled_for = timezone.now() + timedelta(seconds=wait_seconds)
                if next_entry.scheduled_for and next_entry.scheduled_for > scheduled_for:
                    scheduled_for = next_entry.scheduled_for
                next_entry.scheduled_for = scheduled_for
                next_entry.save(update_fields=["scheduled_for", "updated_at"])
                wait_seconds = max(0, int((scheduled_for - timezone.now()).total_seconds() + 0.999))
            else:
                recalculate_campaign(campaign.id, finalize=True)
    response = {
        "sent_now": True,
        "attempted_sequence": sequence,
        "attempt_result": result_name,
        "wait_seconds": wait_seconds,
        "finished": not bool(next_entry),
    }
    if result_name == "rate_limited":
        response.update({
            "rate_limited": True,
            "wait_seconds": wait_seconds,
            "message": f"The provider requested a {wait_seconds}-second pause.",
        })
    return response


def preflight_next(campaign_id, owner_id, run_token):
    with transaction.atomic():
        campaign = Campaign.objects.select_for_update().get(
            pk=campaign_id, owner_id=owner_id
        )
        if campaign.run_token != run_token:
            return {"conflict": True}
        if campaign.status != Campaign.Status.CHECKING:
            return {"inactive": True}
        recipient = (
            campaign.dataset.recipients.select_for_update()
            .filter(
                selected=True,
                phone_validation_status__in=["valid", "warning"],
                suppressed=False,
            )
            .filter(
                Q(whatsapp_checked_at__isnull=True)
                | Q(
                    whatsapp_checked_at__lt=timezone.now()
                    - timedelta(seconds=settings.WASENDER_CHECK_CACHE_SECONDS)
                )
            )
            .order_by("original_row_number")
            .first()
        )
        if not recipient:
            campaign.status = Campaign.Status.READY
            campaign.save(update_fields=["status", "updated_at"])
            return {"finished": True}
    state = check_recipient(recipient)
    return {
        "checked_now": True,
        "recipient_id": str(recipient.id),
        "result": state,
        "wait_seconds": settings.WASENDER_CHECK_INTERVAL_SECONDS,
    }
