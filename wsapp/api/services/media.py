from datetime import timedelta
import logging
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from api.models import UploadedMedia
from api.services.wasender import WasenderClient, WasenderError

logger = logging.getLogger(__name__)


def upload_media(media_id):
    media = UploadedMedia.objects.get(pk=media_id)
    media.upload_status = UploadedMedia.Status.UPLOADING
    media.upload_error = ""
    media.save(update_fields=["upload_status", "upload_error", "updated_at"])
    try:
        with media.original_file.open("rb") as source:
            result = WasenderClient().upload_media(
                source, media.mime_type, Path(media.original_filename).suffix.lower()
            )
        media.provider_public_url = (
            result.data.get("publicUrl")
            or result.data.get("data", {}).get("publicUrl", "")
        )
        if not media.provider_public_url:
            raise WasenderError("Provider did not return a media URL.")
        media.provider_url_expires_at = timezone.now() + timedelta(hours=23)
        media.upload_status = UploadedMedia.Status.READY
        media.upload_error = ""
    except WasenderError as exc:
        media.upload_status = UploadedMedia.Status.FAILED
        category = getattr(exc, "category", "provider")
        if category == "unauthorized":
            media.upload_error = "Provider authentication failed."
        elif category in {"timeout", "connection"}:
            media.upload_error = "Could not reach the media provider. Please retry."
        elif category == "malformed_response":
            media.upload_error = "The media provider returned an invalid response."
        elif exc.status in {400, 422}:
            media.upload_error = "The provider rejected this media type."
        else:
            media.upload_error = "Provider media upload failed."
        logger.warning(
            "media_upload_failed user_id=%s media_id=%s extension=%s mime=%s category=%s size=%s provider_status=%s error_category=%s method=%s",
            media.owner_id, media.id, Path(media.original_filename).suffix.lower(), media.mime_type,
            media.media_type, media.size, exc.status, category, getattr(exc, "upload_method", "raw"),
        )
    else:
        logger.info(
            "media_upload_ready user_id=%s media_id=%s extension=%s mime=%s category=%s size=%s method=%s",
            media.owner_id, media.id, Path(media.original_filename).suffix.lower(), media.mime_type,
            media.media_type, media.size, getattr(result, "upload_method", "raw"),
        )
    media.save()
    return media


def ensure_media_ready(media):
    with transaction.atomic():
        media = UploadedMedia.objects.select_for_update().get(pk=media.pk)
        if (
            not media.provider_public_url
            or not media.provider_url_expires_at
            or media.provider_url_expires_at <= timezone.now()
        ):
            media = upload_media(media.id)
    if media.upload_status != UploadedMedia.Status.READY:
        raise WasenderError(f"Media upload unavailable: {media.upload_error}")
    return media
