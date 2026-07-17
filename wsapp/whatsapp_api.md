# Wasender API reference (sanitized)

Never place credentials in this file. Requests authenticate with the temporary
server-side Bearer token configured in Django settings.

Base URL: `https://www.wasenderapi.com`

## Endpoints

- `POST /api/send-message` — send text or one media URL field.
- `GET /api/on-whatsapp/{phone_number}` — returns `data.exists`.
- `POST /api/upload` — binary media upload; returns `publicUrl`.
- `PUT /api/messages/{msgId}` — edit eligible text message.
- `GET /api/messages/{msgId}/info` — retrieve message and numeric status.
- `DELETE /api/messages/{msgId}` — delete eligible message.
- `POST /api/messages/{msgId}/resend` — resend a provider-recorded failure.

An accepted send commonly returns:

```json
{"success": true, "data": {"msgId": 100000, "jid": "example", "status": "in_progress"}}
```

`in_progress` means accepted or queued, not delivered.
