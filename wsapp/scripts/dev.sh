#!/usr/bin/env bash
set -euo pipefail

if ! command -v redis-server >/dev/null 2>&1; then
  echo "Redis is missing. Install redis-server before starting Waya." >&2
  exit 1
fi
if ! command -v redis-cli >/dev/null 2>&1; then
  echo "redis-cli is missing. Install Redis command-line tools." >&2
  exit 1
fi

redis-cli ping >/dev/null 2>&1 || redis-server --daemonize yes
redis-cli ping | grep -q PONG || { echo "Redis did not start." >&2; exit 1; }

echo "Redis is ready. Start these in separate terminals:"
echo "  celery -A config worker --loglevel=INFO --pool=solo"
echo "  celery -A config beat --loglevel=INFO"
echo "  python manage.py runserver 0.0.0.0:8000"
echo
python manage.py messaging_health || true
