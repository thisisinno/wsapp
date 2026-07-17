# Waya — Excel to WhatsApp

Waya is a Django 5 multi-user campaign application for importing Excel/CSV
recipient data, editing and normalizing phone numbers, personalizing messages,
uploading media, sending through Wasender, and tracking webhook delivery states.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver 0.0.0.0:8000
```

Copy the values you need from `.env.example` into your shell or process manager.
The application does not automatically load an `.env` file. SQLite is used when
`DATABASE_URL` is empty.

## Sending model

Sending is intentionally browser-driven. Start prepares an immutable recipient
snapshot but sends nothing. While the campaign page remains open, the browser
asks Django to process one recipient per request. Django enforces per-user
serialization and provider spacing in database transactions, then records the
real Wasender response before the browser advances.

If the page or tab closes, automatic continuation pauses safely. Reopen the
campaign and click **Resume** to continue from the first queued recipient.
Already attempted recipients are not sent again.

Provider acceptance is distinct from the later sent, delivered, read, and played
states supplied by signed webhooks.

## Checks

```bash
python manage.py check
python manage.py makemigrations --check
python manage.py test
```

All automated provider calls are mocked. A separate guarded
`test_wasender_live` management command remains available for deliberate manual
integration diagnostics only.

## Operational notes

- Serve uploaded media from private/authenticated storage in production.
- Configure the provider to sign the raw webhook body with HMAC-SHA256 in
  `X-Webhook-Signature` (plain hex or an optional `sha256=` prefix).
- Webhooks update delivery state synchronously and monotonically.
