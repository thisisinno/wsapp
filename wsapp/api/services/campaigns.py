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
from api.services.wasender import WasenderClient, WasenderError

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


def _interval_seconds():
    return max(
        60 if settings.WASENDER_TRIAL_MODE else 0,
        settings.WASENDER_SEND_INTERVAL_SECONDS,
    )


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


def _safe_provider_payload(payload):
    """Keep provider diagnostics while ensuring credentials can never be persisted."""
    secret = str(settings.WASENDER_API_KEY or "")

    def clean(value):
        if isinstance(value, dict):
            return {
                str(key): clean(item)
                for key, item in value.items()
                if str(key).lower() not in {"authorization", "api_key", "apikey", "token"}
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, str) and secret:
            return value.replace(secret, "[redacted]")
        return value

    return clean(payload if isinstance(payload, (dict, list)) else {})


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
            MessageAttempt.objects.filter(campaign_recipient__campaign__owner_id=owner_id)
            .order_by("-created_at")
            .first()
        )
        if latest:
            next_allowed = latest.created_at + timedelta(seconds=_interval_seconds())
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
            locked.provider_message_id = str(data.get("msgId", ""))
            locked.provider_jid = str(data.get("jid", ""))
            locked.provider_status_text = str(data.get("status", "accepted"))
            locked.last_provider_payload = safe_payload
            locked.attempt_finished_at = timezone.now()
            locked.skip_reason = ""
            locked.save()
            attempt.http_status = provider.http_status
            attempt.provider_response = safe_payload
            attempt.provider_message_id = locked.provider_message_id
            attempt.duration_ms = int((time.monotonic() - started) * 1000)
            attempt.save()
        result_name = "accepted"
    except WasenderError as exc:
        with transaction.atomic():
            locked = CampaignRecipient.objects.select_for_update().get(pk=entry.pk)
            locked.state = CampaignRecipient.State.FAILED
            locked.failed_at = timezone.now()
            locked.attempt_finished_at = timezone.now()
            locked.skip_reason = str(exc)[:255]
            locked.save()
            attempt.http_status = exc.status
            attempt.provider_response = _safe_provider_payload(exc.payload)
            attempt.error_category = exc.category
            attempt.error_message = str(exc)[:1000]
            attempt.duration_ms = int((time.monotonic() - started) * 1000)
            attempt.save()
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
            wait_seconds = _interval_seconds() if next_entry else 0
            if next_entry:
                next_entry.scheduled_for = timezone.now() + timedelta(seconds=wait_seconds)
                next_entry.save(update_fields=["scheduled_for", "updated_at"])
            else:
                recalculate_campaign(campaign.id, finalize=True)
    return {
        "sent_now": True,
        "attempted_sequence": sequence,
        "attempt_result": result_name,
        "wait_seconds": wait_seconds,
        "finished": not bool(next_entry),
    }


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
