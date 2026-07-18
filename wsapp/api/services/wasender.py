import json
import base64
from dataclasses import dataclass

import requests
from django.conf import settings
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class WasenderError(Exception):
    category = "provider"
    def __init__(self, message, status=None, payload=None, retry_after=None):
        super().__init__(message); self.status = status; self.payload = payload or {}; self.retry_after = retry_after
class UnauthorizedError(WasenderError): category = "unauthorized"
class ValidationError(WasenderError): category = "validation"
class RateLimitError(WasenderError): category = "rate_limit"
class ConnectionError(WasenderError): category = "connection"
class TimeoutError(WasenderError): category = "timeout"
class MalformedResponseError(WasenderError): category = "malformed_response"


@dataclass
class ProviderResult:
    data: dict
    http_status: int


def _safe_message(value):
    message = str(value)
    secret = str(settings.WASENDER_API_KEY or "")
    return message.replace(secret, "[redacted]") if secret else message


def safe_provider_payload(payload):
    """Return useful provider diagnostics with credentials recursively removed."""
    secret = str(settings.WASENDER_API_KEY or "")
    secret_keys = {"authorization", "api_key", "apikey", "token", "access_token"}

    def clean(value):
        if isinstance(value, dict):
            return {
                str(key): clean(item)
                for key, item in value.items()
                if str(key).lower() not in secret_keys
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, str):
            return value.replace(secret, "[redacted]") if secret else value
        return value

    return clean(payload if isinstance(payload, (dict, list)) else {})


def safe_provider_text(value):
    return safe_provider_payload({"message": str(value)}).get("message", "")


ACK_LEVELS = {
    "unknown": -1,
    "failed": 0,
    "pending": 1,
    "sent": 2,
    "delivered": 3,
    "read": 4,
    "played": 5,
}


def normalize_message_status(code=None, text=None):
    """Map provider acknowledgements conservatively to a recipient state."""
    numeric = None
    try:
        numeric = int(code)
    except (TypeError, ValueError):
        pass
    by_code = {
        0: "failed",
        1: "pending",
        2: "sent",
        3: "delivered",
        4: "read",
        5: "played",
    }
    if numeric in by_code:
        return by_code[numeric]
    try:
        text_code = int(text)
    except (TypeError, ValueError):
        text_code = None
    if text_code in by_code:
        return by_code[text_code]
    normalized = str(text or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "in_progress": "pending",
        "processing": "pending",
        "accepted": "pending",
        "queued": "pending",
        "pending": "pending",
        "sent": "sent",
        "server_acknowledged": "sent",
        "delivered": "delivered",
        "read": "read",
        "played": "played",
        "failed": "failed",
        "failure": "failed",
        "error": "failed",
    }
    return aliases.get(normalized, "unknown")


class WasenderClient:
    def __init__(self, api_key=None, base_url=None, session=None):
        self.api_key = settings.WASENDER_API_KEY if api_key is None else api_key
        self.base_url = (base_url or settings.WASENDER_API_BASE_URL).rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        })
        retry = Retry(total=2, connect=2, read=0, status=2, status_forcelist=(502, 503, 504), allowed_methods=frozenset({"GET"}), backoff_factor=.3)
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def _request(self, method, path, **kwargs):
        kwargs.setdefault("timeout", (settings.WASENDER_CONNECT_TIMEOUT, settings.WASENDER_READ_TIMEOUT))
        try:
            response = self.session.request(method, f"{self.base_url}{path}", **kwargs)
        except requests.Timeout as exc:
            raise TimeoutError("Provider request timed out.") from exc
        except requests.ConnectionError as exc:
            raise ConnectionError("Could not connect to provider.") from exc
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            payload = {"message": response.text[:500]} if response.text else {}
            if response.ok:
                raise MalformedResponseError("Provider returned a non-JSON response.", response.status_code)
        message = _safe_message(payload.get("message") or payload.get("error") or f"Provider returned HTTP {response.status_code}")
        if response.status_code == 401: raise UnauthorizedError("Provider authentication failed.", 401, payload)
        if response.status_code == 422: raise ValidationError(message, 422, payload)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After") or payload.get("retry_after")
            raise RateLimitError(message, 429, payload, retry_after)
        if response.status_code >= 400: raise WasenderError(message, response.status_code, payload)
        if not isinstance(payload, dict): raise MalformedResponseError("Malformed provider payload.", response.status_code)
        return ProviderResult(payload, response.status_code)

    def send_message(self, phone, text="", media_field=None, media_url=None, file_name=None):
        payload = {"to": phone}
        if text: payload["text"] = text
        if media_field and media_url: payload[media_field] = media_url
        if media_field == "documentUrl" and media_url and file_name:
            payload["fileName"] = str(file_name).replace("\\", "/").rsplit("/", 1)[-1]
        result = self._request("POST", "/api/send-message", json=payload)
        if result.data.get("success") is not True:
            message = result.data.get("message") or result.data.get("error") or "Provider rejected the message."
            raise WasenderError(_safe_message(message), result.http_status, result.data)
        return result
    def check_number(self, phone): return self._request("GET", f"/api/on-whatsapp/{phone}")
    def upload_media_raw(self, fileobj, mime):
        fileobj.seek(0)
        return self._request("POST", "/api/upload", headers={"Content-Type": mime}, data=fileobj)

    def upload_media(self, fileobj, mime, extension=""):
        """Upload media once as bytes; OOXML validation failures get one fallback."""
        try:
            result = self.upload_media_raw(fileobj, mime)
            self._validate_upload_result(result)
            result.upload_method = "raw"
            return result
        except WasenderError as exc:
            exc.upload_method = "raw"
            if not self._can_base64_fallback(exc, extension, fileobj):
                raise
            fileobj.seek(0)
            encoded = base64.b64encode(fileobj.read()).decode("ascii")
            fileobj.seek(0)
            try:
                result = self._request(
                    "POST", "/api/upload",
                    json={"mimetype": mime, "base64": encoded},
                )
                self._validate_upload_result(result)
            except WasenderError as fallback_exc:
                fallback_exc.upload_method = "base64_fallback"
                raise
            result.upload_method = "base64_fallback"
            return result

    @staticmethod
    def _upload_url(result):
        data = result.data.get("data", result.data)
        return data.get("publicUrl", "") if isinstance(data, dict) else ""

    def _validate_upload_result(self, result):
        if result.data.get("success") is not True or not self._upload_url(result):
            message = result.data.get("message") or result.data.get("error") or "Provider did not return a media URL."
            raise WasenderError(_safe_message(message), result.http_status, result.data)

    @staticmethod
    def _can_base64_fallback(exc, extension, fileobj):
        size = getattr(fileobj, "size", None)
        if size is None:
            position = fileobj.tell()
            fileobj.seek(0, 2)
            size = fileobj.tell()
            fileobj.seek(position)
        return (
            extension.lower() in {".xlsx", ".docx"}
            and exc.status in {400, 422}
            and size <= settings.MEDIA_BASE64_FALLBACK_MAX_BYTES
        )
    def _successful_action(self, method, path, **kwargs):
        result = self._request(method, path, **kwargs)
        if result.data.get("success") is not True:
            message = result.data.get("message") or result.data.get("error") or "Provider rejected the action."
            raise WasenderError(_safe_message(message), result.http_status, result.data)
        return result

    def edit_message(self, message_id, text):
        return self._successful_action("PUT", f"/api/messages/{message_id}", json={"text": text})

    def message_info(self, message_id):
        return self._successful_action("GET", f"/api/messages/{message_id}/info")

    def delete_message(self, message_id):
        return self._successful_action("DELETE", f"/api/messages/{message_id}")

    def resend_message(self, message_id):
        return self._successful_action("POST", f"/api/messages/{message_id}/resend")
