from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from api.models import CampaignRecipient


class Command(BaseCommand):
    help = "Backfill provider message IDs from attempts without sending messages."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        repaired = unavailable = 0
        rows = CampaignRecipient.objects.filter(
            state__in=["accepted", "pending", "sent", "delivered"], provider_message_id=""
        )
        for row in rows.iterator():
            message_id = ""
            for attempt in row.attempts.order_by("-created_at"):
                message_id = attempt.provider_message_id or ""
                if not message_id and isinstance(attempt.provider_response, dict):
                    data = attempt.provider_response.get("data", {})
                    if isinstance(data, dict):
                        message_id = str(data.get("msgId") or data.get("id") or "")
                if message_id:
                    break
            if message_id:
                repaired += 1
                if not options["dry_run"]:
                    with transaction.atomic():
                        locked = CampaignRecipient.objects.select_for_update().get(pk=row.pk)
                        if not locked.provider_message_id:
                            locked.provider_message_id = message_id
                            locked.next_status_check_at = timezone.now()
                            locked.status_sync_error = ""
                            locked.save(update_fields=["provider_message_id", "next_status_check_at", "status_sync_error", "updated_at"])
            else:
                unavailable += 1
                if not options["dry_run"]:
                    row.status_sync_error = "Provider message ID is unavailable; live status sync cannot run."
                    row.save(update_fields=["status_sync_error", "updated_at"])
        mode = "Would repair" if options["dry_run"] else "Repaired"
        self.stdout.write(f"{mode} {repaired} recipient(s); {unavailable} still lack a provider message ID.")
