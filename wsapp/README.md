# Waya — Excel to WhatsApp

A Django 5, server-rendered, multi-user campaign system for importing arbitrary
Excel/CSV recipient data, normalizing Tanzania phone numbers, personalizing
messages, running WhatsApp preflight checks, and sending through a serialized
Celery queue with webhook status tracking.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Export values from `.env` with your process manager or shell. `.env` is ignored;
the application does not automatically load it. SQLite is used when
`DATABASE_URL` is empty. PostgreSQL example:
`postgresql://user:password@localhost:5432/waya`.

## Codespaces/development services

Normal sending requires Redis and a live Celery worker. Run `bash scripts/dev.sh`
for checks and commands, or use separate terminals:

```bash
redis-server --daemonize yes
redis-cli ping
celery -A config worker --loglevel=INFO --pool=solo
celery -A config beat --loglevel=INFO
python manage.py runserver 0.0.0.0:8000
python manage.py messaging_health
```

`APP_ASYNC_MODE=eager-dev` uses an in-memory cache and eager Celery execution
only for development/test utilities. It does not reproduce one-minute
background scheduling and must not be used as the normal sending mode. Trial
mode schedules one dispatcher at a time with at least 60 seconds between
provider attempts; it never sleeps in a web request.

## Tests and live command

```bash
python manage.py check
python manage.py test
ALLOW_LIVE_WASENDER_TEST=1 WASENDER_API_KEY=... \
  python manage.py test_wasender_live --phone +255629645877
```

The live command is deliberately restricted to one existence check and at most
one send. Normal tests mock the provider.

## Operational notes

- Serve uploaded media from private/authenticated storage in production rather
  than Django's development media route.
- Configure the provider to sign the exact raw webhook body with HMAC-SHA256 and
  send it in `X-Webhook-Signature` (plain hex or `sha256=` prefix).
- Run pending-message reconciliation from Celery beat at a conservative cadence.
