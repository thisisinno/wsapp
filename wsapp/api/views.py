import hashlib
import json
import uuid
from pathlib import Path

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.db import models, transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST
from openpyxl import Workbook

from .models import Campaign, CampaignRecipient, ImportedRecipient, MessageTemplate, UploadedDataset, UploadedMedia
from .services.imports import checksum, workbook_sheets
from .services.campaigns import (
    ATTEMPTED_STATES,
    SENDABLE_STATES,
    preflight_next,
    queue_campaign_entries,
    recalculate_campaign,
    resume_campaign,
    send_next,
    start_campaign,
)
from .services.datasets import normalize_dataset, process_dataset
from .services.media import upload_media
from .services.phones import normalize_tanzania_phone
from .services.templates import TemplateError, render_message, validate_template
from .services.wasender import WasenderClient, WasenderError


def ok(data=None, message=""):
    return JsonResponse({"ok": True, "message": message, "data": data or {}})


def error(message, fields=None, status=400, data=None):
    return JsonResponse({"ok": False, "message": message, "errors": fields or {}, "data": data or {}}, status=status)


def json_body(request):
    try: return json.loads(request.body or "{}")
    except json.JSONDecodeError: raise ValidationError("Invalid JSON body.")


@login_required
def dashboard(request):
    campaigns = Campaign.objects.filter(owner=request.user)
    sent = sum(c.sent_count for c in campaigns)
    delivered = sum(c.delivered_count for c in campaigns)
    context = {
        "dataset_count": request.user.datasets.count(), "recipient_count": request.user.recipients.count(),
        "sent_count": sent, "delivery_rate": round(delivered / sent * 100, 1) if sent else 0,
        "campaigns": campaigns.order_by("-created_at")[:6],
        "failures": CampaignRecipient.objects.filter(campaign__owner=request.user, state="failed").select_related("campaign", "imported_recipient")[:6],
    }
    return render(request, "api/dashboard.html", context)


@login_required
@ensure_csrf_cookie
def uploads(request):
    return render(request, "api/uploads.html", {"datasets": request.user.datasets.order_by("-created_at")})


@login_required
@require_POST
def upload_create(request):
    file = request.FILES.get("file")
    if not file: return error("Choose a file.", {"file": ["This field is required."]})
    suffix = Path(file.name).suffix.lower()
    if suffix not in {".xlsx", ".xls", ".csv"}: return error("Unsupported file type.", {"file": ["Use .xlsx, .xls or .csv."]})
    if file.size > settings.DATASET_MAX_BYTES: return error("File is too large.", {"file": [f"Maximum {settings.DATASET_MAX_BYTES} bytes."]})
    digest = checksum(file)
    dataset = UploadedDataset.objects.create(owner=request.user, original_file=file, original_filename=Path(file.name).name[:255], file_type=suffix[1:], size=file.size, checksum=digest)
    try:
        with dataset.original_file.open("rb") as source: sheets = workbook_sheets(source, suffix)
    except Exception as exc:
        dataset.processing_status, dataset.processing_error = "failed", str(exc)
        dataset.save()
        return error(str(exc))
    if len(sheets) > 1 and not request.POST.get("sheet_name"):
        return ok({"id": str(dataset.id), "sheets": sheets, "requires_sheet": True})
    sheet = request.POST.get("sheet_name") or sheets[0]
    process_dataset(str(dataset.id), sheet, int(request.POST.get("header_row", 1)))
    dataset.refresh_from_db()
    if dataset.processing_status == UploadedDataset.Status.FAILED:
        return error(dataset.processing_error or "Workbook processing failed.")
    return ok({"id": str(dataset.id), "url": f"/uploads/{dataset.id}/", "requires_sheet": False, "status": dataset.processing_status}, "Workbook processed.")


@login_required
@ensure_csrf_cookie
def upload_detail(request, dataset_id):
    dataset = get_object_or_404(UploadedDataset, pk=dataset_id, owner=request.user)
    return render(request, "api/dataset.html", {"dataset": dataset})


@login_required
@require_POST
def select_sheet(request, dataset_id):
    dataset = get_object_or_404(UploadedDataset, pk=dataset_id, owner=request.user)
    data = json_body(request)
    sheet = str(data.get("sheet_name", ""))
    with dataset.original_file.open("rb") as source:
        sheets = workbook_sheets(source, f".{dataset.file_type}")
    if sheet not in sheets: return error("Unknown worksheet.", {"sheet_name": ["Choose a detected worksheet."]})
    process_dataset(str(dataset.id), sheet, int(data.get("header_row", 1)))
    dataset.refresh_from_db()
    if dataset.processing_status == UploadedDataset.Status.FAILED:
        return error(dataset.processing_error or "Worksheet processing failed.")
    return ok({"url": f"/uploads/{dataset.id}/", "status": dataset.processing_status}, "Worksheet imported.")


@login_required
@require_GET
def columns(request, dataset_id):
    dataset = get_object_or_404(UploadedDataset, pk=dataset_id, owner=request.user)
    samples = {}
    rows = list(dataset.recipients.order_by("original_row_number").values_list("row_data", flat=True)[:5])
    for column in dataset.detected_columns:
        samples[column["key"]] = [row.get(column["key"], "") for row in rows]
    return ok({"status": dataset.processing_status, "error": dataset.processing_error, "columns": dataset.detected_columns, "samples": samples, "row_count": dataset.row_count})


@login_required
@require_POST
def select_phone_column(request, dataset_id):
    dataset = get_object_or_404(UploadedDataset, pk=dataset_id, owner=request.user)
    data = json_body(request)
    column = data.get("column")
    if column not in {c["key"] for c in dataset.detected_columns}: return error("Unknown column.", {"column": ["Choose a detected column."]})
    normalize_dataset(str(dataset.id), column)
    return ok(recipient_counts(dataset), "Numbers normalized.")


def recipient_counts(dataset):
    values = dict(dataset.recipients.values("phone_validation_status").annotate(n=Count("id")).values_list("phone_validation_status", "n"))
    return {**values, "duplicate": dataset.recipients.exclude(duplicate_of=None).count(), "suppressed": dataset.recipients.filter(suppressed=True).count(), "selected": dataset.recipients.filter(selected=True).count()}


@login_required
@require_GET
def recipients(request, dataset_id):
    dataset = get_object_or_404(UploadedDataset, pk=dataset_id, owner=request.user)
    queryset = dataset.recipients.order_by("original_row_number")
    filter_name = request.GET.get("filter", "all")
    filters = {
        "valid": Q(phone_validation_status__in=["valid", "warning"]), "invalid": Q(phone_validation_status__in=["invalid", "blank"]),
        "exists": Q(whatsapp_state="exists"), "not_exists": Q(whatsapp_state="not_exists"),
        "unchecked": Q(whatsapp_state__in=["unknown", "error"]), "duplicate": Q(duplicate_of__isnull=False),
        "suppressed": Q(suppressed=True), "failed": Q(campaign_entries__state="failed"),
    }
    if filter_name in filters: queryset = queryset.filter(filters[filter_name])
    page, size = max(1, int(request.GET.get("page", 1))), min(100, max(1, int(request.GET.get("page_size", 25))))
    total = queryset.distinct().count()
    rows = queryset.distinct()[(page - 1) * size:page * size]
    data = [{"id": str(r.id), "row_number": r.original_row_number, "row": r.row_data, "phone_original": r.phone_original, "phone_normalized": r.phone_normalized, "validation": r.phone_validation_status, "error": r.validation_error_message, "selected": r.selected, "whatsapp": r.whatsapp_state, "duplicate": bool(r.duplicate_of_id), "suppressed": r.suppressed} for r in rows]
    return ok({"rows": data, "page": page, "page_size": size, "total": total, "counts": recipient_counts(dataset)})


@login_required
@require_POST
def edit_phone(request, recipient_id):
    recipient = get_object_or_404(ImportedRecipient, pk=recipient_id, owner=request.user)
    data = json_body(request); new = data.get("phone", "")
    result = normalize_tanzania_phone(new)
    old = recipient.phone_original
    recipient.phone_original, recipient.phone_normalized = result.original, result.normalized
    recipient.phone_validation_status, recipient.validation_error_code = result.status, result.code
    recipient.validation_error_message, recipient.auto_corrected = result.message, result.auto_corrected
    recipient.row_data[recipient.phone_source_column] = new
    recipient.suppressed = bool(result.normalized and request.user.suppressions.filter(normalized_phone=result.normalized).exists())
    recipient.save()
    return ok({"id": str(recipient.id), "old": old, "new": new, "normalized": result.normalized, "validation": result.status, "error": result.message, "counts": recipient_counts(recipient.dataset)})


@login_required
@require_POST
def bulk_edit_phones(request, dataset_id):
    dataset = get_object_or_404(UploadedDataset, pk=dataset_id, owner=request.user)
    items = json_body(request).get("items", [])
    results = []
    with transaction.atomic():
        for item in items:
            recipient = get_object_or_404(dataset.recipients, pk=item.get("id"))
            result = normalize_tanzania_phone(item.get("phone", ""))
            recipient.phone_original, recipient.phone_normalized = result.original, result.normalized
            recipient.phone_validation_status, recipient.validation_error_code = result.status, result.code
            recipient.validation_error_message, recipient.auto_corrected = result.message, result.auto_corrected
            recipient.row_data[recipient.phone_source_column] = item.get("phone", "")
            recipient.save()
            results.append({"id": str(recipient.id), "normalized": result.normalized, "validation": result.status, "error": result.message})
    return ok({"items": results, "counts": recipient_counts(dataset)}, "Corrections saved.")


@login_required
@require_POST
def selection(request, dataset_id):
    dataset = get_object_or_404(UploadedDataset, pk=dataset_id, owner=request.user)
    data = json_body(request); action = data.get("action")
    qs = dataset.recipients.all()
    if action == "set":
        ids = data.get("ids", [])
        qs.filter(id__in=ids).update(selected=bool(data.get("selected")))
    elif action == "clear": qs.update(selected=False)
    elif action == "all": qs.update(selected=True)
    elif action == "matching":
        filter_name = data.get("filter", "all")
        mapping = {"valid": Q(phone_validation_status__in=["valid", "warning"]), "invalid": Q(phone_validation_status__in=["invalid", "blank"]), "exists": Q(whatsapp_state="exists"), "duplicate": Q(duplicate_of__isnull=False), "suppressed": Q(suppressed=True)}
        qs.update(selected=False)
        qs.filter(mapping.get(filter_name, Q())).update(selected=True)
    else: return error("Unknown selection action.")
    return ok({"selected": dataset.recipients.filter(selected=True).count()})


@login_required
def campaigns(request):
    return render(request, "api/campaigns.html", {"campaigns": request.user.campaigns.order_by("-created_at")})


@login_required
@ensure_csrf_cookie
def campaign_new(request, dataset_id):
    dataset = get_object_or_404(UploadedDataset, pk=dataset_id, owner=request.user)
    return render(request, "api/campaign_form.html", {"dataset": dataset})


@login_required
@require_POST
def campaign_create(request, dataset_id):
    dataset = get_object_or_404(UploadedDataset, pk=dataset_id, owner=request.user)
    data = json_body(request)
    if not data.get("opt_in_confirmed"): return error("Confirm recipient opt-in before sending.", {"opt_in_confirmed": ["Required."]})
    allowed = {c["key"] for c in dataset.detected_columns}
    try: detected = validate_template(data.get("body", ""), allowed)
    except TemplateError as exc: return error(str(exc), {"body": [str(exc)]})
    media = None
    if data.get("media_id"):
        media = get_object_or_404(UploadedMedia, pk=data["media_id"], owner=request.user)
    campaign = Campaign.objects.create(owner=request.user, dataset=dataset, name=data.get("name", "Untitled campaign")[:150], body_snapshot=data.get("body", ""), selected_phone_column=dataset.selected_phone_column, missing_value_policy=data.get("missing_value_policy", "empty"), missing_value_fallback=data.get("missing_value_fallback", ""), allow_duplicates=bool(data.get("allow_duplicates")), allow_unknown=bool(data.get("allow_unknown")), opt_in_confirmed=True, media=media, status=Campaign.Status.READY, send_config_snapshot={"trial_mode": settings.WASENDER_TRIAL_MODE, "interval": settings.WASENDER_SEND_INTERVAL_SECONDS, "placeholders": detected})
    return ok({"id": str(campaign.id), "url": f"/campaigns/{campaign.id}/"})


@login_required
@require_POST
def media_create(request):
    file = request.FILES.get("file")
    if not file: return error("Choose a media file.", {"file": ["Required."]})
    mime = (file.content_type or "").lower()
    types = {
        "image/jpeg": "image", "image/png": "image", "image/webp": "image",
        "video/mp4": "video", "audio/mpeg": "audio", "audio/ogg": "audio",
        "application/pdf": "document", "application/msword": "document",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "document",
    }
    if mime not in types: return error("Unsupported media type.", {"file": ["Use JPEG, PNG, WebP, MP4, MP3, OGG, PDF, DOC or DOCX."]})
    if file.size > settings.MEDIA_MAX_BYTES: return error("Media is too large.")
    digest = hashlib.sha256()
    for chunk in file.chunks(): digest.update(chunk)
    file.seek(0)
    media = UploadedMedia.objects.create(owner=request.user, original_file=file, original_filename=Path(file.name).name[:255], mime_type=mime, media_type=types[mime], size=file.size, checksum=digest.hexdigest())
    media = upload_media(str(media.id))
    if media.upload_status == UploadedMedia.Status.FAILED:
        return error(media.upload_error or "Provider media upload failed.", data={"id": str(media.id), "status": media.upload_status})
    return ok({"id": str(media.id), "status": media.upload_status}, "Media retained and uploaded.")


@login_required
@ensure_csrf_cookie
def campaign_detail(request, campaign_id):
    campaign = get_object_or_404(Campaign, pk=campaign_id, owner=request.user)
    return render(request, "api/campaign_detail.html", {"campaign": campaign})


@login_required
@require_POST
def campaign_preview(request, campaign_id):
    campaign = get_object_or_404(Campaign, pk=campaign_id, owner=request.user)
    selected = campaign.dataset.recipients.filter(selected=True)[:5]
    previews = []
    for r in selected:
        text, missing = render_message(campaign.body_snapshot, r.row_data, campaign.missing_value_policy, campaign.missing_value_fallback)
        previews.append({"recipient_id": str(r.id), "phone": r.phone_normalized, "text": text, "missing": missing})
    return ok({"previews": previews})


@login_required
@require_POST
def campaign_preflight(request, campaign_id):
    campaign = get_object_or_404(Campaign, pk=campaign_id, owner=request.user)
    eligible = campaign.dataset.recipients.filter(
        selected=True, phone_validation_status__in=["valid", "warning"], suppressed=False
    )
    count = min(eligible.count(), settings.WASENDER_MAX_CHECKS_PER_CAMPAIGN)
    if not count:
        return error("No eligible recipients are available for preflight.")
    campaign.run_token = uuid.uuid4().hex
    campaign.status = Campaign.Status.CHECKING
    campaign.save(update_fields=["run_token", "status", "updated_at"])
    return ok(
        {"run_token": campaign.run_token, "total": count, "checked": 0},
        "Preflight ready.",
    )


@login_required
@require_POST
def campaign_preflight_next(request, campaign_id):
    campaign = get_object_or_404(Campaign, pk=campaign_id, owner=request.user)
    result = preflight_next(
        campaign.id, request.user.id, json_body(request).get("run_token", "")
    )
    if result.get("conflict"):
        return error("This preflight token is no longer valid.", status=409)
    return ok(result)


@login_required
@require_POST
def campaign_action(request, campaign_id, action):
    if action not in {"start", "pause", "resume", "cancel"}:
        return error("Unknown campaign action.", status=400)
    campaign = get_object_or_404(Campaign, pk=campaign_id, owner=request.user)
    if action == "start":
        if not campaign.opt_in_confirmed:
            return error("Recipient opt-in confirmation is required.")
        if not campaign.dataset.recipients.filter(selected=True).exists():
            return error("Select at least one recipient before starting.")
        if not settings.WASENDER_API_KEY:
            return error("WhatsApp API key is not configured.", {"configuration": ["Set WASENDER_API_KEY."]})
        campaign = start_campaign(campaign)
        if not campaign.campaign_recipients.filter(state="queued").exists():
            return error("There are no sendable queued recipients.", {"recipients": ["Review skipped and invalid recipient reasons."]}, data=campaign_progress_data(campaign))
        data = campaign_progress_data(campaign)
        data["run_token"] = campaign.run_token
        data["wait_seconds"] = data["seconds_until_next"] or 0
        return ok(data, "Campaign ready to send.")
    elif action == "pause":
        campaign.status = Campaign.Status.PAUSED
        campaign.run_token = uuid.uuid4().hex
        campaign.save(update_fields=["status", "run_token", "updated_at"])
    elif action == "resume":
        if not campaign.campaign_recipients.filter(state="queued").exists():
            return error("No unsent recipients remain to resume.")
        campaign = resume_campaign(campaign)
        data = campaign_progress_data(campaign)
        data["run_token"] = campaign.run_token
        return ok(data, "Campaign ready to resume.")
    elif action == "cancel":
        campaign.status = Campaign.Status.CANCELLED
        campaign.run_token = uuid.uuid4().hex
        campaign.save(update_fields=["status", "run_token", "updated_at"])
        campaign.campaign_recipients.filter(state="queued").update(state="cancelled")
        recalculate_campaign(campaign.id, finalize=False)
    campaign.refresh_from_db()
    return ok(campaign_progress_data(campaign))


@login_required
@require_POST
def campaign_send_next(request, campaign_id):
    campaign = get_object_or_404(Campaign, pk=campaign_id, owner=request.user)
    result = send_next(
        campaign.id, request.user.id, json_body(request).get("run_token", "")
    )
    if result.get("conflict"):
        return error("This campaign run token is no longer valid.", status=409)
    campaign.refresh_from_db()
    data = campaign_progress_data(campaign)
    data.update(result)
    if result.get("busy"):
        data["sent_now"] = False
        data["wait_seconds"] = result["retry_after"]
    elif "wait_seconds" in result and not result.get("sent_now"):
        data["sent_now"] = False
    message = (
        f"Recipient {result['attempted_sequence']} processed."
        if result.get("attempted_sequence")
        else ""
    )
    return ok(data, message)


def mask_phone(phone):
    if len(phone) < 7:
        return "***"
    return f"{phone[:4]}***{phone[-4:]}"


def campaign_progress_data(campaign):
    recipients = campaign.campaign_recipients.select_related("imported_recipient")
    states = dict(recipients.values("state").annotate(n=Count("id")).values_list("state", "n"))
    total = sum(states.values())
    sendable_total = sum(states.get(state, 0) for state in SENDABLE_STATES)
    processed = sum(states.get(state, 0) for state in ATTEMPTED_STATES)
    attempted = recipients.filter(Q(attempt_started_at__isnull=False) | Q(attempts__isnull=False)).distinct().count()
    processing = recipients.filter(state="processing").order_by("sequence_number").first()
    scheduled = recipients.filter(state="queued", scheduled_for__isnull=False).order_by("scheduled_for").first()
    now = timezone.now()
    next_send_at = scheduled.scheduled_for if scheduled else None
    seconds = max(0, int((next_send_at - now).total_seconds())) if next_send_at else None
    order = recipients.annotate(priority=models.Case(
        models.When(state="processing", then=0), models.When(state="failed", then=1), default=2,
        output_field=models.IntegerField(),
    )).order_by("priority", "sequence_number", "-updated_at")[:100]
    rows = [{
        "id": str(r.id), "recipient_id": str(r.imported_recipient_id), "sequence": r.sequence_number,
        "phone_masked": mask_phone(r.normalized_phone), "preview": r.rendered_message[:160],
        "state": r.state, "state_label": r.get_state_display(), "error": r.skip_reason,
        "attempts": r.attempts.count(), "updated_at": r.updated_at.isoformat(),
    } for r in order]
    accepted = states.get("accepted", 0) + states.get("pending", 0)
    latest_attempt = recipients.filter(attempt_started_at__isnull=False).order_by(
        "-attempt_started_at"
    ).first()
    current = processing.sequence_number if processing else (
        latest_attempt.sequence_number if latest_attempt else None
    )
    next_queued = recipients.filter(state="queued").order_by("sequence_number").first()
    media_needs_upload = bool(
        campaign.media_id
        and (
            not campaign.media.provider_public_url
            or not campaign.media.provider_url_expires_at
            or campaign.media.provider_url_expires_at <= now
        )
    )
    return {
        "status": campaign.status, "status_label": campaign.get_status_display(), "total": total,
        "sendable_total": sendable_total, "processed": processed, "completed": processed, "attempted": attempted,
        "remaining": max(sendable_total - processed, 0),
        "current_number": current, "next_sequence": next_queued.sequence_number if next_queued else None,
        "progress_text": f"{processed}/{sendable_total}",
        "percent": round(processed / sendable_total * 100, 1) if sendable_total else 0,
        "queued": states.get("queued", 0), "processing": states.get("processing", 0),
        "accepted": accepted, "sent": states.get("sent", 0), "delivered": states.get("delivered", 0),
        "read": states.get("read", 0) + states.get("played", 0), "failed": states.get("failed", 0),
        "skipped": states.get("skipped", 0), "invalid": states.get("invalid", 0),
        "cancelled": states.get("cancelled", 0), "next_send_at": next_send_at.isoformat() if next_send_at else None,
        "seconds_until_next": seconds,
        "can_start": campaign.status in {"draft", "ready"},
        "can_resume": campaign.status == "paused" and bool(next_queued),
        "can_pause": campaign.status == "sending",
        "can_cancel": campaign.status in {"sending", "paused"} and bool(next_queued),
        "provider_configured": bool(settings.WASENDER_API_KEY),
        "media_needs_upload": media_needs_upload,
        "recipients": rows,
        "latest_result": ({
            "sequence": latest_attempt.sequence_number,
            "state": latest_attempt.state,
            "message": latest_attempt.skip_reason,
        } if latest_attempt else None),
    }


@login_required
@require_GET
def campaign_progress(request, campaign_id):
    campaign = get_object_or_404(Campaign, pk=campaign_id, owner=request.user)
    response = ok(campaign_progress_data(campaign))
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_POST
def message_action(request, entry_id, action):
    entry = get_object_or_404(CampaignRecipient, pk=entry_id, campaign__owner=request.user)
    data = json_body(request)
    if not entry.provider_message_id: return error("The provider has not assigned a message ID.")
    try:
        client = WasenderClient()
        if action == "edit":
            text = data.get("text", "").strip()
            if not text: return error("Message text is required.", {"text": ["Required."]})
            result = client.edit_message(entry.provider_message_id, text); entry.rendered_message = text
        elif action == "delete":
            result = client.delete_message(entry.provider_message_id); entry.state = "cancelled"
        elif action == "resend":
            result = client.resend_message(entry.provider_message_id); entry.state = "accepted"; entry.retry_count += 1
        entry.last_provider_payload = result.data; entry.save()
        return ok({"state": entry.state}, "Provider accepted the action.")
    except WasenderError as exc:
        return error(str(exc), status=exc.status if exc.status and exc.status < 500 else 502)


@login_required
@require_POST
def resend_failed(request, campaign_id):
    campaign = get_object_or_404(Campaign, pk=campaign_id, owner=request.user)
    entries = campaign.campaign_recipients.filter(state="failed")
    count = entries.count()
    if not count:
        return error("No failed recipients are available to resend.")
    requeued = 0
    for entry in entries.select_related("imported_recipient"):
        source = entry.imported_recipient
        rendered, missing = render_message(
            campaign.body_snapshot, source.row_data, campaign.missing_value_policy, campaign.missing_value_fallback
        )
        if source.phone_validation_status not in {"valid", "warning"}:
            entry.state = CampaignRecipient.State.INVALID
            entry.skip_reason = source.validation_error_message or "Invalid corrected phone"
        elif not rendered:
            entry.state = CampaignRecipient.State.SKIPPED
            entry.skip_reason = f"Missing values: {', '.join(missing)}"
        else:
            entry.state = CampaignRecipient.State.QUEUED
            entry.normalized_phone = source.phone_normalized
            entry.rendered_message = rendered
            entry.skip_reason = ""
            requeued += 1
        entry.scheduled_for = entry.attempt_started_at = entry.attempt_finished_at = entry.failed_at = None
        entry.save()
    if not requeued:
        recalculate_campaign(campaign.id)
        return error("No corrected failed recipients are sendable.")
    campaign = resume_campaign(campaign)
    recalculate_campaign(campaign.id, finalize=False)
    data = campaign_progress_data(Campaign.objects.get(pk=campaign.id))
    data["run_token"] = campaign.run_token
    data["requeued"] = requeued
    return ok(data, f"{requeued} failed recipients requeued.")


@login_required
def export_campaign(request, campaign_id):
    campaign = get_object_or_404(Campaign, pk=campaign_id, owner=request.user)
    rows = campaign.campaign_recipients.select_related("imported_recipient")
    if request.GET.get("failed") == "1": rows = rows.filter(state__in=["failed", "invalid", "skipped"])
    wb, ws = Workbook(), None
    ws = wb.active; ws.title = "Campaign results"
    ws.append(["row", "phone", "state", "reason", "provider_message_id", "message"])
    for item in rows: ws.append([item.imported_recipient.original_row_number, item.normalized_phone, item.state, item.skip_reason, item.provider_message_id, item.rendered_message])
    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response["Content-Disposition"] = f'attachment; filename="campaign-{campaign.id}.xlsx"'
    wb.save(response)
    return response
