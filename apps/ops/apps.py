"""App config only.

This module is imported while the app registry is still being populated, so it must not import
models — or anything that imports models. That is why ``default_site`` is a string: Django
resolves it later, after every app is ready.
"""

from django.contrib.admin.apps import AdminConfig


class OpsAdminConfig(AdminConfig):
    """Replaces ``django.contrib.admin`` so ``admin.site`` is our console.

    Every existing ``@admin.register`` keeps working untouched, because they all register
    against ``admin.site``, which is now an :class:`~apps.ops.admin_site.OpsAdminSite`.
    """

    default_site = "apps.ops.admin_site.OpsAdminSite"
