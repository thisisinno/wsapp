from celery.exceptions import TimeoutError as CeleryTimeoutError
from django.conf import settings
from django.core.cache import cache
from redis import Redis

from config.celery import app


def _safe_message(exc):
    text = str(exc).replace(settings.REDIS_URL, "Redis")
    broker = str(settings.CELERY_BROKER_URL or "")
    return text.replace(broker, "messaging broker")[:200]


def check_redis():
    if settings.APP_ASYNC_MODE == "eager-dev":
        return True, "Eager development mode"
    try:
        client = Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=1, socket_timeout=1)
        return bool(client.ping()), ""
    except Exception as exc:
        return False, _safe_message(exc)


def check_cache():
    try:
        key = "waya:health"
        cache.set(key, "ok", timeout=5)
        return cache.get(key) == "ok", ""
    except Exception as exc:
        return False, _safe_message(exc)


def check_celery_worker():
    if settings.APP_ASYNC_MODE == "eager-dev":
        return True, "Eager development mode (no background spacing)"
    try:
        replies = app.control.ping(timeout=1.0)
        return bool(replies), "" if replies else "No Celery worker answered within one second"
    except (CeleryTimeoutError, Exception) as exc:
        return False, _safe_message(exc)


def get_messaging_health():
    broker_ok, broker_error = check_redis()
    cache_ok, cache_error = check_cache()
    worker_ok, worker_error = check_celery_worker() if broker_ok else (False, "Worker check skipped because broker is unavailable")
    ready = broker_ok and cache_ok and worker_ok
    errors = [x for x in (broker_error, cache_error, worker_error) if x]
    message = "Messaging infrastructure is ready." if ready else "Messaging queue is unavailable. Start Redis and the Celery worker, then retry."
    return {"broker_ok": broker_ok, "cache_ok": cache_ok, "worker_ok": worker_ok, "ready": ready, "message": message, "errors": errors}
