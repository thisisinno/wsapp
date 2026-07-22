import os
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from api.models import LiveIntegrationResult
from api.services.wasender import WasenderClient, WasenderError
from api.services.phones import normalize_tanzania_phone


class Command(BaseCommand):
    help = "Perform exactly one guarded Wasender existence check and, if present, one send."

    def add_arguments(self, parser):
        parser.add_argument("--phone", required=True)
        parser.add_argument("--send-one", action="store_true")

    def handle(self, *args, **options):
        if os.getenv("ALLOW_LIVE_WASENDER_TEST") != "1":
            raise CommandError("Live test blocked: ALLOW_LIVE_WASENDER_TEST must equal 1.")
        if not settings.WASENDER_API_KEY:
            raise CommandError("Live test blocked: WASENDER_API_KEY is not configured.")
        normalized = normalize_tanzania_phone(options["phone"])
        phone = normalized.normalized
        if phone != "+255629645877":
            raise CommandError("This controlled command only permits the designated test number.")
        result_row = LiveIntegrationResult(phone_masked="+255******877")
        try:
            check = WasenderClient().check_number(phone)
            result_row.check_http_status = check.http_status
            result_row.exists = check.exists
            if not result_row.exists:
                result_row.save()
                self.stdout.write(f"normalized=+255******877 check_http={check.http_status} exists=false send=skipped")
                return
            if not options["send_one"]:
                result_row.save()
                self.stdout.write(f"normalized=+255******877 check_http={check.http_status} exists=true")
                return
            sent = WasenderClient().send_message(phone, "WhatsApp Excel system integration test successful.")
            data = sent.data.get("data", {})
            result_row.send_http_status = sent.http_status
            result_row.success = bool(sent.data.get("success"))
            result_row.provider_message_id = str(data.get("msgId", ""))
            result_row.provider_state = str(data.get("status", ""))
            result_row.response_summary = {"success": result_row.success, "state": result_row.provider_state}
            result_row.save()
            self.stdout.write(f"normalized=+255******877 check_http={check.http_status} send_http={sent.http_status} success={str(result_row.success).lower()} provider_message_id_present={str(bool(result_row.provider_message_id)).lower()}")
        except WasenderError as exc:
            result_row.response_summary = {"error_category": exc.category, "http_status": exc.status}
            result_row.save()
            raise CommandError(f"Live test failed safely: category={exc.category} http={exc.status or '-'}")
