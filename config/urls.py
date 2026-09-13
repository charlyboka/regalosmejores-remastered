"""URL configuration for the Regalosmejores project."""

from django.contrib import admin
from django.urls import include, path

from apps.web.views import healthz

urlpatterns = [
    path("healthz/", healthz, name="healthz"),
    path("admin/", admin.site.urls),
    path("", include("apps.web.urls")),
]
