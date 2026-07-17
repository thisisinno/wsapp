from datetime import timedelta

from django.utils import timezone

from api.models import UploadedMedia
from api.services.wasender import WasenderClient, WasenderError


def upload_media(media_id):
    media = UploadedMedia.objects.get(pk=media_id)
    media.upload_status = UploadedMedia.Status.UPLOADING
    media.upload_error = ""
    media.save(update_fields=["upload_status", "upload_error", "updated_at"])
    try:
        with media.original_file.open("rb") as source:
            result = WasenderClient().upload_media(source, media.mime_type)
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
        media.upload_error = str(exc)[:1000]
    media.save()
    return media


def ensure_media_ready(media):
    if (
        not media.provider_public_url
        or not media.provider_url_expires_at
        or media.provider_url_expires_at <= timezone.now()
    ):
        media = upload_media(media.id)
    if media.upload_status != UploadedMedia.Status.READY:
        raise WasenderError(f"Media upload unavailable: {media.upload_error}")
    return media
