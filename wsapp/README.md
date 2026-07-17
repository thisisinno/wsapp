# Waya — Excel to WhatsApp

Waya is a Django 5 multi-user campaign application for importing Excel/CSV
recipient data, editing and normalizing Tanzanian phone numbers, personalizing
messages, uploading media, and sending through Wasender.

## Development

After installing `requirements.txt` and applying migrations, the only command
needed to run the application is:

```bash
python manage.py runserver 0.0.0.0:9000
```

Open the application through the forwarded port 9000 URL. Local HTTP/HTTPS port
9000 and GitHub Codespaces `app.github.dev` origins are trusted for Django CSRF.
The temporary private-trial Wasender key is configured server-side in
`config/settings.py`; it is never sent to the browser. No `.env` file is needed
for that key. SQLite is used when `DATABASE_URL` is empty.

## Sending model

Sending is intentionally browser-driven. Start creates an immutable recipient
snapshot but sends nothing. The browser asks Django to process one recipient per
request. Django serializes claims in database transactions, makes one provider
call, records the real response, and returns progress. In trial mode the browser
waits through the visible 60-second countdown before requesting the next send.

If the page or tab closes, reopen the campaign and click **Resume** to continue
from the first queued recipient. Already successful recipients are not requeued.
Provider acceptance means Wasender accepted or queued the message; it does not
prove delivery or reading.

No Redis, Celery, worker, broker, Channels, WebSocket, or webhook process is
required.

## Checks

```bash
python manage.py makemigrations --check
python manage.py migrate
python manage.py check
python manage.py test
```

All automated provider calls are mocked. The guarded `test_wasender_live`
management command is only for deliberate manual integration diagnostics.

Serve uploaded media from private/authenticated storage in production.
