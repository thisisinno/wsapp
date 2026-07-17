import io
import json
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.conf import settings
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook, load_workbook

from .models import (
    Campaign,
    CampaignRecipient,
    ImportedRecipient,
    MessageAttempt,
    UploadedDataset,
    UploadedMedia,
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
        send.return_value = ProviderResult({"success": True, "data": {"msgId": "m", "status": "in_progress"}}, 200)
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
        send.return_value = ProviderResult({"success": True, "data": {"msgId": "m"}}, 200)
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
        send.return_value = ProviderResult({"success": True, "data": {"msgId": "m"}}, 200)
        first = self.start().json()["data"]
        second = self.start().json()["data"]
        self.assertNotEqual(first["run_token"], second["run_token"])
        self.assertEqual(self.campaign.campaign_recipients.count(), 4)
        self.next(second["run_token"])
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
        self.assertEqual(self.next(token).status_code, 409)
        self.client.post(
            reverse("campaign_action", args=[self.campaign.id, "cancel"]),
            data="{}",
            content_type="application/json",
        )
        self.assertEqual(send.call_count, 0)

    @patch("api.services.campaigns.WasenderClient.send_message")
    def test_send_does_not_require_number_precheck(self, send):
        send.return_value = ProviderResult({"success": True, "data": {"msgId": "m"}}, 200)
        token = self.start().json()["data"]["run_token"]
        data = self.next(token).json()["data"]
        self.assertEqual(data["accepted"], 1)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(
            self.campaign.campaign_recipients.order_by("sequence_number")
            .first()
            .state,
            "accepted",
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

    @patch("api.services.campaigns.WasenderClient.send_message")
    def test_unexpected_exception_marks_failed_and_never_leaks_key(self, send):
        send.side_effect = RuntimeError(
            f"unexpected detail {settings.WASENDER_API_KEY}"
        )
        token = self.start().json()["data"]["run_token"]
        with self.assertLogs("api.services.campaigns", level="ERROR") as captured:
            data = self.next(token).json()["data"]
        entry = self.campaign.campaign_recipients.order_by("sequence_number").first()
        entry.refresh_from_db()
        self.assertEqual((data["failed"], entry.state), (1, "failed"))
        self.assertIsNotNone(entry.attempt_finished_at)
        self.assertNotIn(settings.WASENDER_API_KEY, json.dumps(data))
        # logger.exception includes the original exception, so errors must not
        # interpolate provider payloads or credentials into exception messages.
        self.assertIn("Unexpected provider processing failure", captured.output[0])

    @patch("api.services.campaigns.WasenderClient.send_message")
    def test_busy_claim_does_not_make_provider_call(self, send):
        token = self.start().json()["data"]["run_token"]
        first = self.campaign.campaign_recipients.order_by("sequence_number").first()
        first.state = CampaignRecipient.State.PROCESSING
        first.attempt_started_at = timezone.now()
        first.save()
        data = self.next(token).json()["data"]
        self.assertTrue(data["busy"])
        self.assertEqual(send.call_count, 0)


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
            self.response(200, {"success": True, "data": {"msgId": 1}})
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


class CsrfAndFrontendTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("csrf", password="pw")
        self.dataset = make_dataset(self.user)
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.user)

    def payload(self):
        return json.dumps({
            "name": "CSRF",
            "body": "Hello",
            "opt_in_confirmed": True,
        })

    def test_port_9000_is_trusted_and_valid_ajax_create_succeeds(self):
        self.assertIn("https://localhost:9000", settings.CSRF_TRUSTED_ORIGINS)
        page = self.client.get(reverse("campaign_new", args=[self.dataset.id]))
        self.assertEqual(page.status_code, 200)
        token = self.client.cookies["csrftoken"].value
        response = self.client.post(
            reverse("campaign_create", args=[self.dataset.id]),
            self.payload(),
            content_type="application/json",
            HTTP_ORIGIN="https://localhost:9000",
            HTTP_X_CSRFTOKEN=token,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Campaign.objects.filter(owner=self.user).count(), 1)

    def test_missing_csrf_is_rejected_without_creating_campaign(self):
        response = self.client.post(
            reverse("campaign_create", args=[self.dataset.id]),
            self.payload(),
            content_type="application/json",
            HTTP_ORIGIN="https://localhost:9000",
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Campaign.objects.filter(owner=self.user).exists())

    def test_javascript_handles_html_403_and_uses_same_origin_credentials(self):
        source = (Path(settings.BASE_DIR) / "static/api/app.js").read_text()
        self.assertIn('credentials: "same-origin"', source)
        self.assertIn('contentType.includes("application/json")', source)
        self.assertIn("Security check failed. Refresh the page and retry.", source)
        self.assertNotIn("const payload = await response.json();", source)

    def test_webhook_url_is_removed(self):
        self.assertEqual(self.client.post("/webhooks/wasender/").status_code, 404)


class ProviderAcceptanceTests(ProviderClientTests):
    def test_2xx_success_false_is_failure(self):
        with self.assertRaises(WasenderError):
            self.client_for(
                self.response(200, {"success": False, "message": "not accepted"})
            ).send_message("+255712345678", "Hi")

    def test_authorization_is_session_only_and_payload_is_minimal(self):
        session = Mock()
        session.headers = {}
        session.request.return_value = self.response(
            200, {"success": True, "data": {"msgId": 1}}
        )
        WasenderClient(
            api_key="server-secret",
            base_url="https://example.invalid",
            session=session,
        ).send_message("+255712345678", "Hi")
        self.assertEqual(session.headers["Authorization"], "Bearer server-secret")
        kwargs = session.request.call_args.kwargs
        self.assertNotIn("headers", kwargs)
        self.assertEqual(kwargs["json"], {"to": "+255712345678", "text": "Hi"})
