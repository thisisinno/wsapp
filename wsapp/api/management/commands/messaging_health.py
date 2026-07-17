from django.core.management.base import BaseCommand

from api.services.infrastructure import get_messaging_health


class Command(BaseCommand):
    help = "Check messaging broker, cache, and Celery worker readiness."

    def handle(self, *args, **options):
        health = get_messaging_health()
        for key in ("broker_ok", "cache_ok", "worker_ok", "ready"):
            self.stdout.write(f"{key}: {health[key]}")
        self.stdout.write(health["message"])
        if not health["ready"]:
            raise SystemExit(1)
