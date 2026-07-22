import os

from django.core.management.base import BaseCommand, CommandError

from api.models import CampaignRecipient
from api.services.message_logs import apply_provider_status
from api.services.wasender import WasenderClient, normalize_message_status


class Command(BaseCommand):
    help = "Perform exactly one safe provider message-info GET; it never sends a message."

    def add_arguments(self, parser):
        parser.add_argument("--entry", required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        if os.getenv("ALLOW_LIVE_STATUS_CHECK") != "1":
            raise CommandError("Set ALLOW_LIVE_STATUS_CHECK=1 to run this diagnostic.")
        entry = CampaignRecipient.objects.get(pk=options["entry"])
        if not entry.provider_message_id:
            raise CommandError("Recipient has no provider message ID.")
        result = WasenderClient().message_info(entry.provider_message_id)
        data = result.data.get("data", {}) if isinstance(result.data, dict) else {}
        code = data.get("ack", data.get("status", data.get("statusCode"))) if isinstance(data, dict) else None
        state = normalize_message_status(code, code)
        self.stdout.write(f"stored_state={entry.state}\nprovider_code={code}\nprovider_state={state}\nwould_advance={state in {'sent', 'delivered', 'read', 'played'} and state != entry.state}")
        if options["apply"]:
            from django.db import transaction
            with transaction.atomic():
                entry = CampaignRecipient.objects.select_for_update().get(pk=entry.pk)
                apply_provider_status(entry, result.data)
