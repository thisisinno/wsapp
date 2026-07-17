import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name, default=False):
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "unsafe-local-development-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
if not DEBUG and SECRET_KEY == "unsafe-local-development-key-change-me":
    raise ImproperlyConfigured("DJANGO_SECRET_KEY is required when DEBUG is false")
ALLOWED_HOSTS = [x.strip() for x in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",") if x.strip()]

INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "api.apps.ApiConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"], "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.debug", "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth", "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"
CSRF_COOKIE_HTTPONLY = False  # Allow JavaScript to access CSRF cookie
CSRF_COOKIE_SAMESITE = 'Lax'
CSRF_TRUSTED_ORIGINS = [
    'https://*.app.github.dev',  # Allow all GitHub Codespaces domains
    'https://localhost:8000',
    'https://localhost:6379',
    'https://localhost:9000',
]

# CORS settings (if using django-cors-headers)
CORS_ALLOWED_ORIGINS = [
    'https://*.app.github.dev',  # Allow all GitHub Codespaces domains
    'https://localhost:8000',
    'https://localhost:9000',
    'https://localhost:6379',
]

database_url = os.getenv("DATABASE_URL", "")
if database_url:
    try:
        import dj_database_url
    except ImportError as exc:
        raise ImproperlyConfigured("dj-database-url is required for DATABASE_URL") from exc
    DATABASES = {"default": dj_database_url.parse(database_url, conn_max_age=60)}
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Dar_es_Salaam"
USE_I18N = USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

APP_ASYNC_MODE = os.getenv("APP_ASYNC_MODE", "celery").strip().lower()
if APP_ASYNC_MODE not in {"celery", "eager-dev"}:
    raise ImproperlyConfigured("APP_ASYNC_MODE must be 'celery' or 'eager-dev'")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
_result_backend = os.getenv("CELERY_RESULT_BACKEND", "").strip()
CELERY_RESULT_BACKEND = _result_backend or None
CELERY_TASK_IGNORE_RESULT = True
if APP_ASYNC_MODE == "eager-dev":
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "waya-eager-dev"}}
    CELERY_TASK_ALWAYS_EAGER = True
else:
    CACHE_URL = os.getenv("CACHE_URL", "redis://localhost:6379/1")
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": CACHE_URL}}
    CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", False)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ENABLE_UTC = True
CELERY_TIMEZONE = TIME_ZONE
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_SOFT_TIME_LIMIT = int(os.getenv("CELERY_TASK_SOFT_TIME_LIMIT", "90"))
CELERY_TASK_TIME_LIMIT = int(os.getenv("CELERY_TASK_TIME_LIMIT", "120"))
CELERY_BEAT_SCHEDULE = {
    "reconcile-pending-messages": {
        "task": "api.tasks.reconcile_pending_messages",
        "schedule": 900.0,
    }
}
WASENDER_API_BASE_URL = os.getenv("WASENDER_API_BASE_URL", "https://www.wasenderapi.com").rstrip("/")
WASENDER_API_KEY = os.getenv("WASENDER_API_KEY", "")
WASENDER_WEBHOOK_SECRET = os.getenv("WASENDER_WEBHOOK_SECRET", "")
WASENDER_TRIAL_MODE = env_bool("WASENDER_TRIAL_MODE", True)
WASENDER_SEND_INTERVAL_SECONDS = int(os.getenv("WASENDER_SEND_INTERVAL_SECONDS", "60"))
WASENDER_CHECK_INTERVAL_SECONDS = int(os.getenv("WASENDER_CHECK_INTERVAL_SECONDS", "2"))
WASENDER_CHECK_CACHE_SECONDS = int(os.getenv("WASENDER_CHECK_CACHE_SECONDS", "604800"))
WASENDER_MAX_CHECKS_PER_CAMPAIGN = int(os.getenv("WASENDER_MAX_CHECKS_PER_CAMPAIGN", "100"))
WASENDER_CONNECT_TIMEOUT = float(os.getenv("WASENDER_CONNECT_TIMEOUT", "5"))
WASENDER_READ_TIMEOUT = float(os.getenv("WASENDER_READ_TIMEOUT", "30"))
DATASET_MAX_BYTES = int(os.getenv("DATASET_MAX_BYTES", str(20 * 1024 * 1024)))
MEDIA_MAX_BYTES = int(os.getenv("MEDIA_MAX_BYTES", str(16 * 1024 * 1024)))
