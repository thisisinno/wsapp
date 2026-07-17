import hashlib
import hmac
import io
import json
from unittest.mock import Mock, patch

import requests
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook, load_workbook

from .models import (
    Campaign,
    CampaignRecipient,
    ImportedRecipient,
    MessageAttempt,
    UploadedDataset,
    UploadedMedia,
    WebhookEvent,
)
from .services.campaigns import check_recipient, queue_campaign_entries
from .services.imports import disambiguate_headers, parse_tabular
from .services.phones import excel_value_to_text, normalize_tanzania_phone
from .services.templates import TemplateError, render_message, validate_template
from .services.wasender import (
    MalformedResponseError,
    ProviderResult,
    RateLimitError,
    UnauthorizedError,
    ValidationError,
    WasenderClient,
    WasenderError,
)
from .services.webhooks import process_webhook

User = get_user_model()


def make_dataset(owner, content=b"phone,name\n0712345678,Ada\n", name="x.csv"):
    return UploadedDataset.objects.create(
        owner=owner,
        original_file=SimpleUploadedFile(name, content),
        original_filename=name,
        file_type=name.rsplit(".", 1)[-1],
        size=len(content),
        checksum="a" * 64,
        processing_status="ready",
        detected_columns=[{"key": "phone", "label": "Phone", "index": 1}],
        selected_phone_column="phone",
    )


class PhoneImportTemplateTests(TestCase):
    def test_normalization_import_and_templates(self):
        for value in [
            "+255712345678",
            "255712345678",
            "0712345678",
            "712345678",
            "0712 345-678",
            "0712345678.0",
        ]:
            self.assertEqual(normalize_tanzania_phone(value).normalized, "+255712345678")
        self.assertEqual(excel_value_to_text(712345678.0), "712345678")
        self.assertEqual(
            [item["key"] for item in disambiguate_headers(["Name", "", "Name"])],
            ["name", "column_2", "name_2"],
        )
        columns, rows = parse_tabular(
            io.BytesIO(b"Name,Phone\nAda,0712345678\n"), "x.csv"
        )
        self.assertEqual((len(columns), rows[0][1]["phone"]), (2, "0712345678"))
        self.assertEqual(validate_template("Hi {name}", {"name"}), ["name"])
        with self.assertRaises(TemplateError):
            validate_template("{missing}", {"name"})
        self.assertEqual(
            render_message("Hi {name} {{ok}}", {"name": "Ada"})[0], "Hi Ada {ok}"
        )


@override_settings(
    WASENDER_API_KEY="test-key-never-used",
    WASENDER_TRIAL_MODE=False,
    WASENDER_SEND_INTERVAL_SECONDS=0,
)
class CampaignSequentialTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("owner", password="pw")
        self.other = User.objects.create_user("other", password="pw")
        self.dataset = make_dataset(self.user)
        for index in range(4):
            ImportedRecipient.objects.create(
                dataset=self.dataset,
                owner=self.user,
                original_row_number=index + 2,
                row_data={"phone": f"071234567{index}", "name": f"Person {index}"},
                phone_source_column="phone",
                phone_original=f"071234567{index}",
                phone_normalized=f"+25571234567{index}",
                phone_validation_status="valid",
            )
        self.campaign = Campaign.objects.create(
            owner=self.user,
            dataset=self.dataset,
            name="Four",
            body_snapshot="Hi {name}",
            selected_phone_column="phone",
            opt_in_confirmed=True,
            allow_unknown=True,
            status=Campaign.Status.READY,
        )
        self.client.force_login(self.user)
        self.start_url = reverse("campaign_action", args=[self.campaign.id, "start"])
        self.next_url = reverse("campaign_send_next", args=[self.campaign.id])

    def start(self):
        return self.client.post(
            self.start_url, data="{}", content_type="application/json"
        )

    def next(self, token):
        return self.client.post(
            self.next_url,
            data=json.dumps({"run_token": token}),
            content_type="application/json",
        )

    @patch("api.services.campaigns.WasenderClient.check_number")
    @patch("api.services.campaigns.WasenderClient.send_message")
    def test_start_and_four_one_item_requests(self, send, check):
        check.return_value = ProviderResult({"data": {"exists": True}}, 200)
        send.return_value = ProviderResult({"data": {"msgId": "m", "status": "accepted"}}, 200)
        first = self.start()
        self.assertEqual(first.status_code, 200)
        data = first.json()["data"]
        self.assertEqual((data["progress_text"], data["total"]), ("0/4", 4))
        self.assertEqual(self.campaign.campaign_recipients.count(), 4)
        self.assertEqual(send.call_count, 0)
        token = data["run_token"]
        for number in range(1, 5):
            response = self.next(token)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["data"]["progress_text"], f"{number}/4")
            self.assertEqual(send.call_count, number)
        self.assertTrue(response.json()["data"]["finished"])

    @patch("api.services.campaigns.WasenderClient.check_number")
    @patch("api.services.campaigns.WasenderClient.send_message")
    def test_validation_failure_is_processed_once(self, send, check):
        check.return_value = ProviderResult({"data": {"exists": True}}, 200)
        send.side_effect = ValidationError("invalid destination", 422, {"safe": True})
        token = self.start().json()["data"]["run_token"]
        data = self.next(token).json()["data"]
        self.assertEqual((data["processed"], data["failed"]), (1, 1))
        self.assertEqual(send.call_count, 1)
        self.assertEqual(MessageAttempt.objects.count(), 1)

    @override_settings(WASENDER_TRIAL_MODE=True, WASENDER_SEND_INTERVAL_SECONDS=0)
    @patch("api.services.campaigns.WasenderClient.check_number")
    @patch("api.services.campaigns.WasenderClient.send_message")
    def test_trial_window_prevents_immediate_second_provider_call(self, send, check):
        check.return_value = ProviderResult({"data": {"exists": True}}, 200)
        send.return_value = ProviderResult({"data": {"msgId": "m"}}, 200)
        token = self.start().json()["data"]["run_token"]
        self.next(token)
        second = self.next(token).json()["data"]
        self.assertFalse(second["sent_now"])
        self.assertGreater(second["wait_seconds"], 0)
        self.assertEqual(send.call_count, 1)

    @patch("api.services.campaigns.WasenderClient.check_number")
    @patch("api.services.campaigns.WasenderClient.send_message")
    def test_idempotent_start_resume_token_and_no_duplicate_accepted(self, send, check):
        check.return_value = ProviderResult({"data": {"exists": True}}, 200)
        send.return_value = ProviderResult({"data": {"msgId": "m"}}, 200)
        first = self.start().json()["data"]
        second = self.start().json()["data"]
        self.assertEqual(first["run_token"], second["run_token"])
        self.assertEqual(self.campaign.campaign_recipients.count(), 4)
        self.next(first["run_token"])
        self.client.post(
            reverse("campaign_action", args=[self.campaign.id, "pause"]),
            data="{}",
            content_type="application/json",
        )
        resumed = self.client.post(
            reverse("campaign_action", args=[self.campaign.id, "resume"]),
            data="{}",
            content_type="application/json",
        ).json()["data"]
        self.next(resumed["run_token"])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(
            self.campaign.campaign_recipients.filter(state="accepted").count(), 2
        )

    @patch("api.services.campaigns.WasenderClient.send_message")
    def test_mismatched_token_pause_cancel_and_non_whatsapp(self, send):
        token = self.start().json()["data"]["run_token"]
        self.assertEqual(self.next("wrong").status_code, 409)
        self.assertEqual(send.call_count, 0)
        pause = self.client.post(
            reverse("campaign_action", args=[self.campaign.id, "pause"]),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(pause.status_code, 200)
        self.assertEqual(self.next(token).json()["data"]["status"], "paused")
        self.client.post(
            reverse("campaign_action", args=[self.campaign.id, "cancel"]),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(send.call_count, 0)

    @patch("api.services.campaigns.WasenderClient.check_number")
    @patch("api.services.campaigns.WasenderClient.send_message")
    def test_not_registered_is_skipped_and_never_sent(self, send, check):
        check.return_value = ProviderResult({"data": {"exists": False}}, 200)
        token = self.start().json()["data"]["run_token"]
        data = self.next(token).json()["data"]
        self.assertEqual(data["skipped"], 1)
        self.assertEqual(send.call_count, 0)
        self.assertEqual(
            self.campaign.campaign_recipients.order_by("sequence_number")
            .first()
            .skip_reason,
            "Not registered on WhatsApp",
        )

    @patch("api.services.campaigns.WasenderClient.check_number")
    def test_fresh_database_check_reused(self, check):
        recipient = self.dataset.recipients.first()
        check.return_value = ProviderResult({"data": {"exists": True}}, 200)
        self.assertEqual(check_recipient(recipient), "exists")
        recipient.refresh_from_db()
        self.assertEqual(check_recipient(recipient), "exists")
        self.assertEqual(check.call_count, 1)

    def test_progress_masks_and_owner_scope(self):
        self.start()
        response = self.client.get(
            reverse("campaign_progress", args=[self.campaign.id])
        )
        self.assertEqual(response["Cache-Control"], "no-store")
        text = response.content.decode()
        self.assertNotIn("+255712345670", text)
        self.assertNotIn("test-key-never-used", text)
        self.client.force_login(self.other)
        for name, method in [
            ("campaign_progress", self.client.get),
            ("campaign_send_next", self.client.post),
            ("campaign_action", self.client.post),
        ]:
            args = (
                [self.campaign.id, "start"]
                if name == "campaign_action"
                else [self.campaign.id]
            )
            self.assertEqual(method(reverse(name, args=args)).status_code, 404)

    def test_cancel_only_changes_queued_and_resend_only_failed(self):
        token = self.start().json()["data"]["run_token"]
        entries = list(self.campaign.campaign_recipients.order_by("sequence_number"))
        entries[0].state = "accepted"
        entries[0].save()
        entries[1].state = "failed"
        entries[1].skip_reason = "bad"
        entries[1].save()
        response = self.client.post(
            reverse("resend_failed", args=[self.campaign.id]),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(response.json()["data"]["requeued"], 1)
        entries[0].refresh_from_db()
        self.assertEqual(entries[0].state, "accepted")
        self.client.post(
            reverse("campaign_action", args=[self.campaign.id, "cancel"]),
            data="{}",
            content_type="application/json",
        )
        entries[0].refresh_from_db()
        self.assertEqual(entries[0].state, "accepted")

    def test_export_and_cross_user_access(self):
        queue_campaign_entries(self.campaign)
        response = self.client.get(reverse("export_campaign", args=[self.campaign.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            load_workbook(io.BytesIO(response.content)).active["A1"].value, "row"
        )


class SynchronousUploadMediaTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("files", password="pw")
        self.client.force_login(self.user)

    def test_csv_upload_completes_synchronously(self):
        response = self.client.post(
            reverse("upload_create"),
            {"file": SimpleUploadedFile("people.csv", b"phone,name\n0712345678,Ada\n")},
        )
        self.assertEqual(response.status_code, 200)
        dataset = UploadedDataset.objects.get(owner=self.user)
        self.assertEqual((dataset.processing_status, dataset.row_count), ("ready", 1))

    @patch("api.services.media.WasenderClient.upload_media")
    def test_media_upload_completes_synchronously(self, upload):
        upload.return_value = ProviderResult({"publicUrl": "https://cdn.invalid/a.jpg"}, 200)
        response = self.client.post(
            reverse("media_create"),
            {"file": SimpleUploadedFile("a.jpg", b"image", content_type="image/jpeg")},
        )
        self.assertEqual(response.status_code, 200)
        media = UploadedMedia.objects.get(owner=self.user)
        self.assertEqual(media.upload_status, "ready")
        self.assertTrue(media.original_file)


class ProviderClientTests(TestCase):
    def response(self, status, payload=None, text=""):
        response = Mock(status_code=status, ok=status < 400, headers={}, text=text)
        if payload is None:
            response.json.side_effect = ValueError()
        else:
            response.json.return_value = payload
        return response

    def client_for(self, response=None, exc=None):
        session = Mock()
        session.request.side_effect = exc
        if not exc:
            session.request.return_value = response
        return WasenderClient(
            api_key="example", base_url="https://example.invalid", session=session
        )

    def test_success_errors_timeout_and_non_json(self):
        result = self.client_for(
            self.response(200, {"data": {"msgId": 1}})
        ).send_message("+255712345678", "Hi")
        self.assertEqual(result.http_status, 200)
        for status, exception in [
            (401, UnauthorizedError),
            (422, ValidationError),
            (429, RateLimitError),
            (500, WasenderError),
        ]:
            with self.assertRaises(exception):
                self.client_for(self.response(status, {"message": "safe"})).check_number(
                    "+255712345678"
                )
        from .services.wasender import TimeoutError

        with self.assertRaises(TimeoutError):
            self.client_for(exc=requests.Timeout()).check_number("+255712345678")
        with self.assertRaises(MalformedResponseError):
            self.client_for(self.response(200, None, "not json")).check_number(
                "+255712345678"
            )


@override_settings(WASENDER_WEBHOOK_SECRET="hook-test-secret")
class WebhookTests(TestCase):
    def setUp(self):
        user = User.objects.create_user("hook")
        data = make_dataset(user)
        recipient = ImportedRecipient.objects.create(
            dataset=data,
            owner=user,
            original_row_number=2,
            row_data={},
            phone_normalized="+255712345678",
            phone_validation_status="valid",
        )
        campaign = Campaign.objects.create(
            owner=user,
            dataset=data,
            name="C",
            body_snapshot="Hi",
            selected_phone_column="phone",
        )
        self.entry = CampaignRecipient.objects.create(
            campaign=campaign,
            imported_recipient=recipient,
            normalized_phone="+255712345678",
            provider_message_id="123",
            state="sent",
        )

    def signed_post(self, body):
        signature = hmac.new(b"hook-test-secret", body, hashlib.sha256).hexdigest()
        return self.client.post(
            reverse("wasender_webhook"),
            body,
            content_type="application/json",
            HTTP_X_WEBHOOK_SIGNATURE=signature,
        )

    def test_sync_signature_idempotency_and_monotonic_state(self):
        body = json.dumps(
            {"event": "messages.update", "data": {"msgId": "123", "status": 4}}
        ).encode()
        self.assertEqual(
            self.client.post(
                reverse("wasender_webhook"), body, content_type="application/json"
            ).status_code,
            401,
        )
        self.assertEqual(self.signed_post(body).status_code, 200)
        self.signed_post(body)
        self.assertEqual(WebhookEvent.objects.count(), 1)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.state, "read")
        older = WebhookEvent.objects.create(
            event_hash="c" * 64,
            signature_valid=True,
            payload={"data": {"msgId": "123", "status": 2}},
        )
        process_webhook(older.id)
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.state, "read")
