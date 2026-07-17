from django.utils import timezone

from api.models import CampaignRecipient, WebhookEvent


STATUS_MAP = {
    0: "failed",
    1: "pending",
    2: "sent",
    3: "delivered",
    4: "read",
    5: "played",
}
STATUS_RANK = {
    "failed": 0,
    "queued": 0,
    "processing": 0,
    "accepted": 1,
    "pending": 1,
    "sent": 2,
    "delivered": 3,
    "read": 4,
    "played": 5,
}


def process_webhook(event_id):
    event = WebhookEvent.objects.get(pk=event_id)
    payload = event.payload
    data = payload.get("data", payload)
    message_id = str(data.get("msgId") or data.get("messageId") or "")
    try:
        code = int(data.get("status"))
    except (TypeError, ValueError):
        code = None
    entry = CampaignRecipient.objects.filter(provider_message_id=message_id).first()
    if entry and code in STATUS_MAP:
        state = STATUS_MAP[code]
        if state == "failed" or STATUS_RANK[state] >= STATUS_RANK.get(entry.state, 0):
            entry.state = state
            entry.provider_status_code = code
            entry.last_provider_payload = payload
            now = timezone.now()
            if state == "sent":
                entry.sent_at = entry.sent_at or now
            if state == "delivered":
                entry.delivered_at = entry.delivered_at or now
            if state in {"read", "played"}:
                entry.read_at = entry.read_at or now
            if state == "failed":
                entry.failed_at = entry.failed_at or now
            entry.save()
            from api.services.campaigns import recalculate_campaign

            recalculate_campaign(entry.campaign_id)
    event.processing_state = "processed"
    event.processing_error = ""
    event.processed_at = timezone.now()
    event.save(
        update_fields=[
            "processing_state",
            "processing_error",
            "processed_at",
            "updated_at",
        ]
    )
    return event
