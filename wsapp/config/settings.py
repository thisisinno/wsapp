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
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SAMESITE = "Lax"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_TRUSTED_ORIGINS = [
    "http://localhost:8000",
    "https://localhost:8000",
    "http://localhost:9000",
    "https://localhost:9000",
    "http://127.0.0.1:8000",
    "https://127.0.0.1:8000",
    "http://127.0.0.1:9000",
    "https://127.0.0.1:9000",
    "https://*.app.github.dev",
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

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "waya-local-only",
    }
}
WASENDER_API_BASE_URL = "https://www.wasenderapi.com"
WASENDER_API_KEY = "0c5c283150371e044f207e7c2a9e7bf44cc135d8615dd417115c23bafd9c4ca3"
WASENDER_TRIAL_MODE = True
WASENDER_SEND_INTERVAL_SECONDS = 2
WASENDER_CHECK_INTERVAL_SECONDS = int(os.getenv("WASENDER_CHECK_INTERVAL_SECONDS", "2"))
WASENDER_CHECK_CACHE_SECONDS = int(os.getenv("WASENDER_CHECK_CACHE_SECONDS", "604800"))
WASENDER_MAX_CHECKS_PER_CAMPAIGN = int(os.getenv("WASENDER_MAX_CHECKS_PER_CAMPAIGN", "100"))
WASENDER_CONNECT_TIMEOUT = 10
WASENDER_READ_TIMEOUT = 45
DATASET_MAX_BYTES = int(os.getenv("DATASET_MAX_BYTES", str(20 * 1024 * 1024)))
MEDIA_MAX_BYTES = int(os.getenv("MEDIA_MAX_BYTES", str(16 * 1024 * 1024)))
