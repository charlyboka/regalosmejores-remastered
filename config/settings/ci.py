"""Settings used only by CI for static validation.

Identical to production security-wise (so ``check --deploy`` is meaningful) but it never
opens a database connection and never reads a local ``.env``.
"""

from .prod import *  # noqa: F403

# collectstatic does not run in CI, so the manifest storage would have nothing to read.
STORAGES = {  # noqa: F405
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
