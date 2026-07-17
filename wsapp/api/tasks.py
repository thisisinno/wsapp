"""Backward-compatible imports for code written before synchronous services.

These are ordinary functions. Runtime work is performed directly by Django
views and the browser-driven campaign endpoints.
"""

from .services.campaigns import (
    queue_campaign_entries,
    recalculate_campaign,
)
from .services.datasets import normalize_dataset, process_dataset
from .services.media import upload_media

__all__ = [
    "normalize_dataset",
    "process_dataset",
    "queue_campaign_entries",
    "recalculate_campaign",
    "upload_media",
]
