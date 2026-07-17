import hashlib
import hmac
import io
import json
import tempfile
from unittest.mock import Mock, patch

import requests
from kombu.exceptions import OperationalError
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook, load_workbook

from .models import Campaign, CampaignRecipient, ImportedRecipient, UploadedDataset, WebhookEvent
from .services.imports import disambiguate_headers, parse_tabular
from .services.phones import excel_value_to_text, normalize_tanzania_phone
from .services.templates import TemplateError, render_message, validate_template
from .services.wasender import (MalformedResponseError, RateLimitError, UnauthorizedError,
                                ValidationError, WasenderClient, WasenderError)
from .tasks import STATUS_RANK, process_webhook, queue_campaign_entries

User = get_user_model()


class PhoneTests(TestCase):
    def test_valid_variants_and_excel_float(self):
        for value in ["+255712345678", "255712345678", "0712345678", "712345678", "0712 345-678", "0712345678.0"]:
            result = normalize_tanzania_phone(value)
            self.assertEqual(result.normalized, "+255712345678")
            self.assertEqual(result.status, "valid")
        self.assertEqual(excel_value_to_text(712345678.0), "712345678")

    def test_edge_cases_and_non_mobile_warning(self):
        for value in ["", None, "07123", "071234567890", "+256712345678", "0712ABC678", "++255712345678"]:
            self.assertIn(normalize_tanzania_phone(value).status, {"blank", "invalid"})
        result = normalize_tanzania_phone("221234567")
        self.assertEqual((result.normalized, result.status), ("+255221234567", "warning"))


class ImportAndTemplateTests(TestCase):
    def test_headers_arbitrary_blank_duplicate(self):
        headers = disambiguate_headers(["Name", "", "Name", "Phone #"])
        self.assertEqual([h["key"] for h in headers], ["name", "column_2", "name_2", "phone"])
        content = io.BytesIO(b"Name,,Name,Phone\nAda,,A,0712345678\n")
        columns, rows = parse_tabular(content, "x.csv")
        self.assertEqual(len(columns), 4)
        self.assertEqual(rows[0][1]["phone"], "0712345678")

    def test_xlsx_types_and_formula_cached_blank(self):
        source = io.BytesIO()
        wb = Workbook(); ws = wb.active
        ws.append(["phone", "active", "joined"]); ws.append([712345678.0, True, None]); wb.save(source); source.seek(0)
        columns, rows = parse_tabular(source, "x.xlsx")
        self.assertEqual(rows[0][1]["phone"], "712345678")
        self.assertIs(rows[0][1]["active"], True)

    def test_placeholder_rendering(self):
        self.assertEqual(validate_template("Hi {name}", {"name"}), ["name"])
        with self.assertRaises(TemplateError): validate_template("{missing}", {"name"})
        self.assertEqual(render_message("Hi {name} {{literal}}", {"name": "Ada"})[0], "Hi Ada {literal}")
        self.assertEqual(render_message("Hi {name}", {}, "fallback", "friend")[0], "Hi friend")
        self.assertIsNone(render_message("Hi {name}", {}, "skip")[0])


def dataset(owner, name="x.csv"):
    return UploadedDataset.objects.create(
        owner=owner, original_file=SimpleUploadedFile(name, b"phone,name\n0712345678,Ada"),
        original_filename=name, file_type="csv", size=30, checksum="a" * 64,
        processing_status="ready", detected_columns=[{"key": "phone", "label": "Phone", "index": 1}],
        selected_phone_column="phone",
    )


class OwnershipSelectionCampaignTests(TestCase):
    def setUp(self):
        self.u1 = User.objects.create_user("one", password="pw")
        self.u2 = User.objects.create_user("two", password="pw")
        self.d1 = dataset(self.u1)
        self.r1 = ImportedRecipient.objects.create(dataset=self.d1, owner=self.u1, original_row_number=2, row_data={"phone": "0712345678", "name": "Ada"}, phone_source_column="phone", phone_original="0712345678", phone_normalized="+255712345678", phone_validation_status="valid")

    def test_cross_user_access_returns_404(self):
        self.client.force_login(self.u2)
        self.assertEqual(self.client.get(reverse("upload_detail", args=[self.d1.id])).status_code, 404)
        self.assertEqual(self.client.post(reverse("edit_phone", args=[self.r1.id]), data="{}", content_type="application/json").status_code, 404)

    def test_selection_matching_server_side(self):
        ImportedRecipient.objects.create(dataset=self.d1, owner=self.u1, original_row_number=3, row_data={}, phone_validation_status="invalid")
        self.client.force_login(self.u1)
        response = self.client.post(reverse("selection", args=[self.d1.id]), data=json.dumps({"action": "matching", "filter": "valid"}), content_type="application/json")
        self.assertTrue(response.json()["ok"])
        self.assertEqual(self.d1.recipients.filter(selected=True).count(), 1)

    def test_edit_single_and_bulk(self):
        self.client.force_login(self.u1)
        response = self.client.post(reverse("edit_phone", args=[self.r1.id]), data=json.dumps({"phone": "0623456789"}), content_type="application/json")
        self.assertEqual(response.json()["data"]["normalized"], "+255623456789")
        response = self.client.post(reverse("bulk_edit_phones", args=[self.d1.id]), data=json.dumps({"items": [{"id": str(self.r1.id), "phone": "0712345678"}]}), content_type="application/json")
        self.assertEqual(response.json()["data"]["items"][0]["validation"], "valid")

    def test_campaign_double_queue_idempotency_and_suppression(self):
        campaign = Campaign.objects.create(owner=self.u1, dataset=self.d1, name="C", body_snapshot="Hi {name}", selected_phone_column="phone", opt_in_confirmed=True)
        queue_campaign_entries(campaign); queue_campaign_entries(campaign)
        self.assertEqual(campaign.campaign_recipients.count(), 1)
        self.assertEqual(campaign.campaign_recipients.get().rendered_message, "Hi Ada")

    def test_export(self):
        campaign = Campaign.objects.create(owner=self.u1, dataset=self.d1, name="C", body_snapshot="Hi", selected_phone_column="phone", opt_in_confirmed=True)
        queue_campaign_entries(campaign)
        self.client.force_login(self.u1)
        response = self.client.get(reverse("export_campaign", args=[campaign.id]))
        self.assertEqual(response.status_code, 200)
        wb = load_workbook(io.BytesIO(response.content))
        self.assertEqual(wb.active["A1"].value, "row")


@override_settings(WASENDER_API_KEY="test-key-never-used")
class CampaignQueueRegressionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("queue-user", password="pw")
        self.data = dataset(self.user)
        for number in range(4):
            ImportedRecipient.objects.create(
                dataset=self.data, owner=self.user, original_row_number=number + 2,
                row_data={"phone": f"071234567{number}", "name": f"Person {number}"},
                phone_source_column="phone", phone_normalized=f"+25571234567{number}",
                phone_validation_status="valid",
            )
        self.campaign = Campaign.objects.create(
            owner=self.user, dataset=self.data, name="Four", body_snapshot="Hi {name}",
            selected_phone_column="phone", opt_in_confirmed=True, allow_unknown=True,
            status=Campaign.Status.READY,
        )
        self.client.force_login(self.user)
        self.url = reverse("campaign_action", args=[self.campaign.id, "start"])
        self.ready = {
            "broker_ok": True, "cache_ok": True, "worker_ok": True, "ready": True,
            "message": "ready", "errors": [],
        }

    @patch("api.views.get_messaging_health")
    def test_unavailable_infrastructure_returns_503_and_never_queues(self, health):
        health.return_value = {**self.ready, "broker_ok": False, "worker_ok": False, "ready": False, "message": "Messaging queue is unavailable.", "errors": ["Redis connection refused"]}
        response = self.client.post(self.url, data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 503)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.READY)
        self.assertTrue(self.campaign.queue_error)
        self.assertFalse(response.json()["data"]["health"]["ready"])

    @patch("api.views.send_next_campaign_recipient.apply_async")
    @patch("api.views.get_messaging_health")
    def test_enqueue_failure_restores_ready_and_second_start_retries(self, health, enqueue):
        health.return_value = self.ready
        enqueue.side_effect = [OperationalError("connection refused"), Mock(id="accepted-task")]
        first = self.client.post(self.url, data="{}", content_type="application/json")
        self.assertEqual(first.status_code, 503)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.READY)
        self.assertEqual(self.campaign.dispatch_task_id, "")
        second = self.client.post(self.url, data="{}", content_type="application/json")
        self.assertEqual(second.status_code, 200)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.QUEUED)
        self.assertEqual(self.campaign.dispatch_task_id, "accepted-task")

    @patch("api.views.send_next_campaign_recipient.apply_async")
    @patch("api.views.get_messaging_health")
    def test_snapshot_exists_before_enqueue_and_double_start_is_idempotent(self, health, enqueue):
        health.return_value = self.ready
        enqueue.return_value = Mock(id="one-task")
        response = self.client.post(self.url, data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["total"], 4)
        self.assertEqual(response.json()["data"]["progress_text"], "0/4")
        self.assertEqual(self.campaign.campaign_recipients.count(), 4)
        again = self.client.post(self.url, data="{}", content_type="application/json")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(enqueue.call_count, 1)

    @patch("api.views.get_messaging_health")
    def test_progress_masks_phone_and_keeps_accepted_distinct(self, health):
        health.return_value = self.ready
        queue_campaign_entries(self.campaign)
        entries = list(self.campaign.campaign_recipients.order_by("sequence_number"))
        entries[0].state = CampaignRecipient.State.ACCEPTED
        entries[0].attempt_started_at = timezone.now()
        entries[0].attempt_finished_at = timezone.now()
        entries[0].save()
        entries[1].state = CampaignRecipient.State.FAILED
        entries[1].attempt_started_at = timezone.now()
        entries[1].attempt_finished_at = timezone.now()
        entries[1].save()
        response = self.client.get(reverse("campaign_progress", args=[self.campaign.id]))
        data = response.json()["data"]
        self.assertEqual((data["completed"], data["accepted"], data["failed"]), (2, 1, 1))
        self.assertEqual(data["delivered"], 0)
        self.assertNotIn("+255712345670", json.dumps(data["recipients"]))
        self.assertEqual(response["Cache-Control"], "no-store")

    @patch("api.views.preflight_next_recipient.apply_async", side_effect=OperationalError("broker down"))
    @patch("api.views.get_messaging_health")
    def test_preflight_enqueue_failure_restores_ready(self, health, enqueue):
        health.return_value = self.ready
        response = self.client.post(reverse("campaign_preflight", args=[self.campaign.id]), data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 503)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.status, Campaign.Status.READY)

    def test_unknown_action_is_rejected(self):
        response = self.client.post(reverse("campaign_action", args=[self.campaign.id, "bogus"]), data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 400)


class ProviderClientTests(TestCase):
    def response(self, status, payload=None, text=""):
        response = Mock(status_code=status, ok=status < 400, headers={}, text=text)
        if payload is None: response.json.side_effect = ValueError()
        else: response.json.return_value = payload
        return response

    def provider_client(self, response=None, exc=None):
        session = Mock()
        if exc: session.request.side_effect = exc
        else: session.request.return_value = response
        return WasenderClient(api_key="example", base_url="https://example.invalid", session=session)

    def test_success_and_error_mapping(self):
        result = self.provider_client(self.response(200, {"success": True, "data": {"msgId": 1}})).send_message("+255712345678", "Hi")
        self.assertEqual(result.http_status, 200)
        for status, exception in [(401, UnauthorizedError), (422, ValidationError), (429, RateLimitError), (500, WasenderError)]:
            with self.assertRaises(exception): self.provider_client(self.response(status, {"message": "safe"})).check_number("+255712345678")

    def test_timeout_and_non_json(self):
        from api.services.wasender import TimeoutError
        with self.assertRaises(TimeoutError): self.provider_client(exc=requests.Timeout()).check_number("+255712345678")
        with self.assertRaises(MalformedResponseError): self.provider_client(self.response(200, None, "not json")).check_number("+255712345678")


@override_settings(WASENDER_WEBHOOK_SECRET="hook-test-secret", CELERY_TASK_ALWAYS_EAGER=True)
class WebhookTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("hook")
        self.data = dataset(self.user)
        recipient = ImportedRecipient.objects.create(dataset=self.data, owner=self.user, original_row_number=2, row_data={}, phone_normalized="+255712345678", phone_validation_status="valid")
        campaign = Campaign.objects.create(owner=self.user, dataset=self.data, name="C", body_snapshot="Hi", selected_phone_column="phone")
        self.entry = CampaignRecipient.objects.create(campaign=campaign, imported_recipient=recipient, normalized_phone="+255712345678", provider_message_id="123", state="sent")

    @patch("api.views.process_webhook.delay")
    def test_signature_and_duplicate_idempotency(self, delay):
        body = json.dumps({"event": "messages.update", "data": {"msgId": "123", "status": 3}}).encode()
        sig = hmac.new(b"hook-test-secret", body, hashlib.sha256).hexdigest()
        url = reverse("wasender_webhook")
        self.assertEqual(self.client.post(url, body, content_type="application/json").status_code, 401)
        self.assertEqual(self.client.post(url, body, content_type="application/json", HTTP_X_WEBHOOK_SIGNATURE=sig).status_code, 200)
        self.client.post(url, body, content_type="application/json", HTTP_X_WEBHOOK_SIGNATURE=sig)
        self.assertEqual(WebhookEvent.objects.count(), 1)
        self.assertEqual(delay.call_count, 1)

    def test_monotonic_status(self):
        event = WebhookEvent.objects.create(event_hash="b" * 64, signature_valid=True, payload={"data": {"msgId": "123", "status": 4}})
        with patch("api.tasks.refresh_campaign.delay"):
            process_webhook(str(event.id))
        self.entry.refresh_from_db(); self.assertEqual(self.entry.state, "read")
        older = WebhookEvent.objects.create(event_hash="c" * 64, signature_valid=True, payload={"data": {"msgId": "123", "status": 2}})
        with patch("api.tasks.refresh_campaign.delay"):
            process_webhook(str(older.id))
        self.entry.refresh_from_db(); self.assertEqual(self.entry.state, "read")
