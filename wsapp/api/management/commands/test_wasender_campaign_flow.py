"""Deliberately narrow, opt-in production integration check; never used by tests."""
import os
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError

from api.models import Campaign, ImportedRecipient, UploadedDataset
from api.services.campaigns import check_recipient, queue_campaign_entries, send_next, start_campaign
from api.services.phones import normalize_tanzania_phone


class Command(BaseCommand):
    help = "Guarded, single-recipient Wasender campaign-flow integration test."

    def add_arguments(self, parser):
        parser.add_argument("--phone", required=True)
        parser.add_argument("--send-one", action="store_true")

    def handle(self, *args, **options):
        if os.getenv("ALLOW_LIVE_WASENDER_TEST") != "1" or not options["send_one"]:
            raise CommandError("Blocked: set ALLOW_LIVE_WASENDER_TEST=1 and pass --send-one.")
        normalized = normalize_tanzania_phone(options["phone"])
        if normalized.normalized != "+255629645877":
            raise CommandError("This controlled command only permits the designated test number.")
        User = get_user_model()
        user, _ = User.objects.get_or_create(username="wasender-integration-test", defaults={"is_active": False})
        dataset = UploadedDataset.objects.create(
            owner=user, original_file=ContentFile(b"phone\n0629645877\n", name="integration-test.csv"),
            original_filename="integration-test.csv", file_type="csv", size=17,
            checksum="live-integration-test", processing_status="ready", selected_phone_column="phone",
        )
        recipient = ImportedRecipient.objects.create(
            dataset=dataset, owner=user, original_row_number=2, row_data={"phone": "0629645877"},
            phone_source_column="phone", phone_original=normalized.original, phone_normalized=normalized.normalized,
            phone_validation_status=normalized.status,
        )
        try:
            check = check_recipient(recipient)
            if check.get("state") != "exists":
                self.stdout.write("normalized=+255******877 whatsapp_exists=false send_calls=0")
                return
            campaign = Campaign.objects.create(owner=user, dataset=dataset, name="LIVE INTEGRATION TEST", body_snapshot="Integration test", selected_phone_column="phone", allow_unknown=False, opt_in_confirmed=True)
            queue_campaign_entries(campaign)
            campaign = start_campaign(campaign)
            send_next(campaign.id, user.id, campaign.run_token)
            campaign.refresh_from_db()
            entry = campaign.campaign_recipients.first()
            self.stdout.write("normalized=+255******877 whatsapp_exists=true campaign_progress=%s/%s send_calls=1 provider_message_id_present=%s" % (1 if entry and entry.attempts.exists() else 0, campaign.total_count, str(bool(entry and entry.provider_message_id)).lower()))
        finally:
            dataset.delete()
