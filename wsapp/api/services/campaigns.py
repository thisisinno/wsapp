import logging
import hashlib
import time
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from api.models import Campaign, CampaignRecipient, ImportedRecipient, MessageAttempt, ProviderSendGate
from api.services.media import ensure_media_ready
from api.services.templates import render_message
from api.services.wasender import WasenderClient, WasenderError, UnauthorizedError, safe_provider_payload, safe_provider_text

logger = logging.getLogger(__name__)


class SendIntervalError(ValueError):
    """Raised when an untrusted send interval is invalid."""


class CheckResult(dict):
    """Structured result with legacy string comparison compatibility."""
    def __eq__(self, other):
        return self.get("state") == other if isinstance(other, str) else super().__eq__(other)


def parse_send_interval(raw_value, *, default=None, allow_blank=False):
    """Strict validation for user supplied values; configuration is normalized separately."""
    if raw_value is None or str(raw_value).strip() == "":
        if allow_blank:
            if default is None:
                raise SendIntervalError("Send interval must be between 5 and 3600 seconds.")
            raw_value = default
        else:
            raise SendIntervalError("Send interval must be between 5 and 3600 seconds.")

    text = str(raw_value).strip()
    if not text.isdigit():
        raise SendIntervalError("Send interval must be between 5 and 3600 seconds.")

    value = int(text)
    if value < 5 or value > 3600:
        raise SendIntervalError("Send interval must be between 5 and 3600 seconds.")
    return value


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


def normalize_send_interval(value, default=5):
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
            return max(5, retry, campaign_interval)
    return max(5, campaign_interval)


def _provider_fingerprint():
    return hashlib.sha256(str(settings.WASENDER_API_KEY or "unconfigured").encode()).hexdigest()


def claim_provider_send_slot(*, requested_interval, campaign_id, recipient_id):
    """Reserve the shared provider slot before an HTTP attempt, never during it."""
    delay = max(5, normalize_send_interval(requested_interval))
    now = timezone.now()
    fingerprint = _provider_fingerprint()
    # get_or_create is deliberately outside the lock; the unique key resolves first-use races.
    ProviderSendGate.objects.get_or_create(provider_key_fingerprint=fingerprint)
    with transaction.atomic():
        gate = ProviderSendGate.objects.select_for_update().get(provider_key_fingerprint=fingerprint)
        if gate.next_allowed_at and gate.next_allowed_at > now:
            wait = max(1, int((gate.next_allowed_at - now).total_seconds() + .999))
            return {"claimed": False, "wait_seconds": wait, "next_allowed_at": gate.next_allowed_at}
        gate.next_allowed_at = now + timedelta(seconds=delay)
        gate.last_attempt_at = now
        gate.last_campaign_id = campaign_id
        gate.last_recipient_id = recipient_id
        gate.save(update_fields=["next_allowed_at", "last_attempt_at", "last_campaign_id", "last_recipient_id", "updated_at"])
    return {"claimed": True, "wait_seconds": 0, "next_allowed_at": now + timedelta(seconds=delay)}


def extend_provider_send_gate(seconds):
    """Keep a provider-requested 429 pause global as well."""
    until = timezone.now() + timedelta(seconds=max(5, int(seconds)))
    with transaction.atomic():
        gate = ProviderSendGate.objects.select_for_update().get(provider_key_fingerprint=_provider_fingerprint())
        if not gate.next_allowed_at or gate.next_allowed_at < until:
            gate.next_allowed_at = until
            gate.save(update_fields=["next_allowed_at", "updated_at"])


def recalculate_campaign(campaign_id, finalize=True):
    campaign = Campaign.objects.get(pk=campaign_id)
    counts = dict(
        campaign.campaign_recipients.values("state")
        .annotate(n=Count("id"))
        .values_list("state", "n")
    )
    campaign.total_count = sum(counts.values())
    campaign.queued_count = counts.get("queued", 0)
    campaign.pending_count = counts.get("accepted", 0) + counts.get("pending", 0)
    campaign.sent_count = sum(
        counts.get(state, 0)
        for state in ("sent", "delivered", "read", "played")
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
            reason = recipient.whatsapp_check_error_message if recipient.whatsapp_state == ImportedRecipient.WhatsApp.ERROR and recipient.whatsapp_check_error_message else "WhatsApp registration has not been confirmed."
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


def _check_backoff(attempts, exc=None):
    if getattr(exc, "status", None) == 429:
        try:
            return max(1, min(int(float(exc.retry_after)), settings.WASENDER_CHECK_MAX_INTERVAL_SECONDS))
        except (TypeError, ValueError):
            pass
    if isinstance(exc, UnauthorizedError):
        return settings.WASENDER_CHECK_MAX_INTERVAL_SECONDS
    return 10 if attempts <= 1 else 30 if attempts == 2 else 60


def whatsapp_checks_due(queryset, now=None):
    """Shared selection rule; recent failures wait for their recorded backoff."""
    now = now or timezone.now()
    fresh = now - timedelta(seconds=settings.WASENDER_CHECK_CACHE_SECONDS)
    stale_claim = now - timedelta(seconds=settings.WASENDER_CONNECT_TIMEOUT + settings.WASENDER_READ_TIMEOUT + 15)
    valid = queryset.filter(phone_validation_status__in=["valid", "warning"]).exclude(phone_normalized="")
    return valid.filter(
        Q(whatsapp_checked_at__isnull=True)
        | Q(whatsapp_state__in=[ImportedRecipient.WhatsApp.EXISTS, ImportedRecipient.WhatsApp.NOT_EXISTS], whatsapp_checked_at__lt=fresh)
        | Q(whatsapp_state=ImportedRecipient.WhatsApp.ERROR, whatsapp_next_check_at__lte=now)
        | Q(whatsapp_state=ImportedRecipient.WhatsApp.CHECKING, whatsapp_checked_at__lt=stale_claim)
    )


def check_recipient(recipient, client=None, retry_now=False):
    """Claim, call outside the transaction, then persist a strict provider result."""
    now = timezone.now()
    fresh = now - timedelta(seconds=settings.WASENDER_CHECK_CACHE_SECONDS)
    with transaction.atomic():
        locked = ImportedRecipient.objects.select_for_update().get(pk=recipient.pk)
        if locked.whatsapp_state in {locked.WhatsApp.EXISTS, locked.WhatsApp.NOT_EXISTS} and locked.whatsapp_checked_at and locked.whatsapp_checked_at >= fresh:
            return CheckResult(state=locked.whatsapp_state, exists=locked.whatsapp_state == locked.WhatsApp.EXISTS, checked=False, cached=True, wait_seconds=0)
        if locked.whatsapp_state == locked.WhatsApp.CHECKING and locked.whatsapp_checked_at and locked.whatsapp_checked_at > now - timedelta(seconds=settings.WASENDER_CONNECT_TIMEOUT + settings.WASENDER_READ_TIMEOUT + 15):
            return CheckResult(state="checking", checked=False, busy=True, wait_seconds=2)
        if not retry_now and locked.whatsapp_state == locked.WhatsApp.ERROR and locked.whatsapp_next_check_at and locked.whatsapp_next_check_at > now:
            return CheckResult(state="error", checked=False, waiting_retry=True, wait_seconds=max(1, int((locked.whatsapp_next_check_at - now).total_seconds() + .999)))
        locked.whatsapp_state = locked.WhatsApp.CHECKING
        locked.whatsapp_checked_at = now
        locked.save(update_fields=["whatsapp_state", "whatsapp_checked_at", "updated_at"])
    try:
        result = (client or WasenderClient()).check_number(locked.phone_normalized)
    except WasenderError as exc:
        with transaction.atomic():
            locked = ImportedRecipient.objects.select_for_update().get(pk=recipient.pk)
            attempts = locked.whatsapp_check_attempts + 1
            delay = _check_backoff(attempts, exc)
            locked.whatsapp_state = locked.WhatsApp.ERROR
            locked.whatsapp_checked_at = timezone.now()
            locked.whatsapp_check_attempts = attempts
            locked.whatsapp_check_error_code = exc.category
            locked.whatsapp_check_error_message = safe_provider_text(exc)[:255] or "Provider registration check failed."
            locked.whatsapp_check_http_status = exc.status
            locked.whatsapp_next_check_at = locked.whatsapp_checked_at + timedelta(seconds=delay)
            locked.whatsapp_last_payload = safe_provider_payload(exc.payload)
            locked.save()
        return CheckResult(state="error", exists=None, checked=True, error_code=exc.category, error_message=locked.whatsapp_check_error_message, http_status=exc.status, retry_after=delay, wait_seconds=delay, authentication_error=isinstance(exc, UnauthorizedError))
    with transaction.atomic():
        locked = ImportedRecipient.objects.select_for_update().get(pk=recipient.pk)
        exists = result.exists if hasattr(result, "exists") else result.data.get("data", {}).get("exists")
        if not isinstance(exists, bool):
            raise WasenderError("Provider returned an invalid registration-check response.")
        locked.whatsapp_state = locked.WhatsApp.EXISTS if exists else locked.WhatsApp.NOT_EXISTS
        locked.whatsapp_checked_at = timezone.now()
        locked.whatsapp_check_attempts += 1
        locked.whatsapp_check_http_status = result.http_status
        locked.whatsapp_last_payload = safe_provider_payload(getattr(result, "provider_payload", result.data))
        locked.whatsapp_check_error_code = locked.whatsapp_check_error_message = ""
        locked.whatsapp_next_check_at = None
        locked.save()
    return CheckResult(state=locked.whatsapp_state, exists=exists, checked=True, error_code="", error_message="", http_status=result.http_status, retry_after=0, wait_seconds=settings.WASENDER_CHECK_INTERVAL_SECONDS)


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
        interval = campaign_interval_seconds(campaign)
        entry = (
            campaign.campaign_recipients.select_for_update()
            .filter(state=CampaignRecipient.State.QUEUED)
            .order_by("sequence_number", "created_at")
            .first()
        )
        if not entry:
            recalculate_campaign(campaign.id)
            return {"finished": True}
        claim = claim_provider_send_slot(
            requested_interval=interval, campaign_id=campaign.id, recipient_id=entry.id
        )
        if not claim["claimed"]:
            return {"wait_seconds": claim["wait_seconds"], "next_allowed_at": claim["next_allowed_at"].isoformat(),
                    "message": "The provider queue requires a 5-second interval."}
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
            payload.update({"media_type": media.media_type, "original_filename": media.original_filename})
            attempt.request_payload = payload
            attempt.save(update_fields=["request_payload", "updated_at"])
        provider = WasenderClient().send_message(
            entry.normalized_phone,
            entry.rendered_message,
            media_field,
            media_url,
            media.original_filename if campaign.media and media_field == "documentUrl" else None,
        )
        from api.services.message_logs import apply_send_result
        apply_send_result(entry.id, provider, attempt_id=attempt.id,
                          finished_at=timezone.now(), duration_ms=int((time.monotonic() - started) * 1000))
        result_name = "accepted"
    except WasenderError as exc:
        with transaction.atomic():
            locked = CampaignRecipient.objects.select_for_update().get(pk=entry.pk)
            is_rate_limited = provider_rate_limited(exc)
            retry_seconds = provider_retry_seconds(exc, campaign_interval_seconds(campaign)) if is_rate_limited else 0
            if is_rate_limited:
                extend_provider_send_gate(retry_seconds)
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
        if campaign.preflight_checked >= campaign.preflight_limit:
            campaign.status = Campaign.Status.READY
            campaign.save(update_fields=["status", "updated_at"])
            return {"finished": True, "limit_reached": True}
        recipient = (
            whatsapp_checks_due(campaign.dataset.recipients.select_for_update())
            .filter(
                selected=True,
                phone_validation_status__in=["valid", "warning"],
                suppressed=False,
            )
            .order_by("original_row_number")
            .first()
        )
        if not recipient:
            campaign.status = Campaign.Status.READY
            campaign.save(update_fields=["status", "updated_at"])
            return {"finished": True}
    result = check_recipient(recipient)
    with transaction.atomic():
        campaign = Campaign.objects.select_for_update().get(pk=campaign_id)
        campaign.preflight_checked = min(campaign.preflight_checked + (1 if result.get("checked") else 0), campaign.preflight_limit)
        campaign.save(update_fields=["preflight_checked", "updated_at"])
    return {
        "checked_now": result.get("checked", False),
        "recipient_id": str(recipient.id),
        "result": result.get("state"),
        "wait_seconds": result.get("wait_seconds", settings.WASENDER_CHECK_INTERVAL_SECONDS),
        "checked": campaign.preflight_checked,
        "total": campaign.preflight_total,
    }
