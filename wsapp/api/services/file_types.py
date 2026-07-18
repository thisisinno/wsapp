"""Canonical, extension-led media validation.

Browser supplied content types are deliberately advisory: OOXML workbooks are
ZIP containers and are commonly labelled as ``application/zip``.
"""
from dataclasses import dataclass
from pathlib import Path
import zipfile

from django.conf import settings


@dataclass(frozen=True)
class ResolvedMediaType:
    extension: str
    canonical_mime: str
    media_type: str
    category: str
    max_bytes: int


SUPPORTED_MEDIA_EXTENSIONS = {
    ".jpg": ("image/jpeg", "image", "image"), ".jpeg": ("image/jpeg", "image", "image"),
    ".png": ("image/png", "image", "image"), ".webp": ("image/webp", "image", "image"),
    ".mp4": ("video/mp4", "video", "video"), ".3gp": ("video/3gpp", "video", "video"),
    ".mp3": ("audio/mpeg", "audio", "audio"), ".ogg": ("audio/ogg", "audio", "audio"),
    ".aac": ("audio/aac", "audio", "audio"), ".amr": ("audio/amr", "audio", "audio"),
    ".pdf": ("application/pdf", "document", "document"),
    ".doc": ("application/msword", "document", "document"),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "document", "document"),
    ".xls": ("application/vnd.ms-excel", "document", "document"),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "document", "document"),
    ".csv": ("text/csv", "document", "document"), ".txt": ("text/plain", "document", "document"),
}

OLE_SIGNATURE = bytes.fromhex("D0 CF 11 E0 A1 B1 1A E1")


class MediaValidationError(ValueError):
    def __init__(self, message, code="invalid_media"):
        super().__init__(message)
        self.code = code


def _limit(category):
    return {
        "image": settings.MEDIA_IMAGE_MAX_BYTES,
        "audio": settings.MEDIA_AUDIO_MAX_BYTES,
        "video": settings.MEDIA_VIDEO_MAX_BYTES,
        "document": settings.MEDIA_DOCUMENT_MAX_BYTES,
    }[category]


def _read_prefix(fileobj, size=8192):
    fileobj.seek(0)
    data = fileobj.read(size)
    fileobj.seek(0)
    return data


def _validate_zip(fileobj, required, label):
    try:
        fileobj.seek(0)
        with zipfile.ZipFile(fileobj) as archive:
            names = set(archive.namelist())
            if not all(name in names for name in required):
                raise MediaValidationError(f"The {label} is not valid.", "invalid_package")
            if archive.testzip() is not None:
                raise MediaValidationError(f"The {label} is corrupt.", "invalid_package")
    except MediaValidationError:
        raise
    except (zipfile.BadZipFile, OSError, EOFError):
        raise MediaValidationError(f"The {label} is not valid.", "invalid_package")
    finally:
        fileobj.seek(0)


def resolve_and_validate_media(fileobj):
    """Return canonical metadata after cheap, non-native content validation."""
    if not fileobj or not getattr(fileobj, "name", ""):
        raise MediaValidationError("Choose a media file.", "required")
    extension = Path(fileobj.name).suffix.lower()
    mapping = SUPPORTED_MEDIA_EXTENSIONS.get(extension)
    if not mapping:
        raise MediaValidationError("Use JPEG, PNG, WebP, MP4, 3GP, MP3, OGG, AAC, AMR, PDF, DOC, DOCX, XLS, XLSX, CSV or TXT.", "unsupported_extension")
    mime, media_type, category = mapping
    if not fileobj.size:
        raise MediaValidationError("The selected file is empty.", "empty_file")
    max_bytes = _limit(category)
    if fileobj.size > max_bytes:
        label = "document" if category == "document" else category
        raise MediaValidationError(f"This {label} is too large. Maximum {label} size is {max_bytes // (1024 * 1024)} MB.", "file_too_large")
    prefix = _read_prefix(fileobj)
    if extension in {".jpg", ".jpeg", ".png", ".webp"} and (
        prefix.startswith((b"%PDF-", b"MZ")) or prefix.startswith(OLE_SIGNATURE)
    ):
        raise MediaValidationError("The image extension does not match the selected file.", "dangerous_mismatch")
    if extension == ".pdf" and not prefix.startswith(b"%PDF-"):
        raise MediaValidationError("The PDF file is not valid.", "invalid_pdf")
    if extension in {".xls", ".doc"} and not prefix.startswith(OLE_SIGNATURE):
        raise MediaValidationError(f"The {extension[1:].upper()} file is not valid.", "invalid_ole")
    if extension == ".xlsx":
        _validate_zip(fileobj, ("[Content_Types].xml", "xl/workbook.xml"), "XLSX workbook")
    if extension == ".docx":
        _validate_zip(fileobj, ("[Content_Types].xml", "word/document.xml"), "DOCX document")
    if extension in {".csv", ".txt"}:
        sample = prefix
        if b"\0" in sample or (sample and sum(byte < 9 or (13 < byte < 32) for byte in sample) / len(sample) > .10):
            raise MediaValidationError("The selected text document appears to be binary.", "invalid_text")
    fileobj.seek(0)
    return ResolvedMediaType(extension, mime, media_type, category, max_bytes)
