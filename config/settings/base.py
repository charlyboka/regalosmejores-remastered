"""Settings shared by every environment.

Environment-specific modules (``dev``, ``prod``, ``ci``) import everything from here and
override only what differs. Values come from environment variables, loaded from ``.env``
when that file exists.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    # override=True so the file is authoritative during local development.
    environ.Env.read_env(_env_file, overwrite=True)


# --------------------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------------------

SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
SITE_URL = env("SITE_URL", default="http://localhost:8000").rstrip("/")

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sitemaps",
    # Project apps. Order matters only for template/static resolution.
    "apps.clients",
    "apps.catalog",
    "apps.topics",
    "apps.search",
    "apps.pipelines",
    "apps.tracking",
    "apps.web",
    "apps.seo",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.web.context_processors.site",
            ],
        },
    },
]


# --------------------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------------------
# Local development points at the production Heroku Postgres on purpose (see blueprint §2).

DATABASES = {"default": env.db("DATABASE_URL")}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DATABASE_CONN_MAX_AGE", default=60)
DATABASES["default"].setdefault("OPTIONS", {})
DATABASES["default"]["OPTIONS"]["sslmode"] = env("DATABASE_SSLMODE", default="require")


# --------------------------------------------------------------------------------------
# Auth / i18n / static
# --------------------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "es-es"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_NAME = "rm_session"
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"


# --------------------------------------------------------------------------------------
# Project configuration (blueprint §11)
# --------------------------------------------------------------------------------------

# --- OpenAI ---
OPENAI_API_KEY = env("OPENAI_API_KEY", default="")
OPENAI_ORGANIZATION = env("OPENAI_ORGANIZATION", default="")
OPENAI_PROJECT = env("OPENAI_PROJECT", default="")
OPENAI_DEFAULT_MODEL = env("OPENAI_DEFAULT_MODEL", default="gpt-5.6-luna")
OPENAI_EMBEDDING_MODEL = env("OPENAI_EMBEDDING_MODEL", default="text-embedding-3-small")
OPENAI_EMBEDDING_DIMENSIONS = env.int("OPENAI_EMBEDDING_DIMENSIONS", default=512)
OPENAI_TIMEOUT_SECONDS = env.int("OPENAI_TIMEOUT_SECONDS", default=60)
OPENAI_MAX_RETRIES = env.int("OPENAI_MAX_RETRIES", default=3)

# --- Keepa ---
KEEPA_API_KEY = env("KEEPA_API_KEY", default="")
KEEPA_DOMAIN_ID = env.int("KEEPA_DOMAIN_ID", default=9)  # 9 = amazon.es
KEEPA_TOKEN_RESERVE = env.int("KEEPA_TOKEN_RESERVE", default=20)
KEEPA_REQUEST_TIMEOUT_SECONDS = env.int("KEEPA_REQUEST_TIMEOUT_SECONDS", default=30)

# --- Telegram ---
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_CHANNEL_ID = env("TELEGRAM_CHANNEL_ID", default="")

# --- Amazon affiliate ---
AMAZON_AFFILIATE_TAG = env("AMAZON_AFFILIATE_TAG", default="")
AMAZON_MARKETPLACE_HOST = env("AMAZON_MARKETPLACE_HOST", default="www.amazon.es")

# --- Pipelines / budgets ---
PIPELINES_ENABLED = env.bool("PIPELINES_ENABLED", default=True)
LLM_DAILY_BUDGET_USD = env.float("LLM_DAILY_BUDGET_USD", default=5.0)
MAX_HYDRATION_BACKLOG = env.int("MAX_HYDRATION_BACKLOG", default=5000)

# --- Privacy ---
SESSION_SALT = env("SESSION_SALT", default="change-me")


# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------

LOG_LEVEL = env("LOG_LEVEL", default="INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "%(asctime)s %(levelname)-8s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain"},
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "propagate": True},
        "httpx": {"level": "WARNING", "propagate": True},
        "apps": {"level": LOG_LEVEL, "propagate": True},
    },
}
