"""Backward-compatible imports for code written before synchronous services.

These are ordinary functions. Runtime work is performed directly by Django
views and the browser-driven campaign endpoints.
"""

from .services.campaigns import (
    check_recipient,
    queue_campaign_entries,
    recalculate_campaign,
)
from .services.datasets import normalize_dataset, process_dataset
from .services.media import upload_media
from .services.webhooks import STATUS_MAP, STATUS_RANK, process_webhook

__all__ = [
    "STATUS_MAP",
    "STATUS_RANK",
    "check_recipient",
    "normalize_dataset",
    "process_dataset",
    "process_webhook",
    "queue_campaign_entries",
    "recalculate_campaign",
    "upload_media",
]
