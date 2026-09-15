"""URL configuration for the Regalosmejores project."""

from django.contrib import admin
from django.contrib.sitemaps.views import sitemap
from django.urls import include, path

from apps.web.sitemaps import SITEMAPS
from apps.web.views import healthz, robots_txt

urlpatterns = [
    path("healthz/", healthz, name="healthz"),
    path("admin/", admin.site.urls),
    path("robots.txt", robots_txt, name="robots"),
    path("sitemap.xml", sitemap, {"sitemaps": SITEMAPS}, name="sitemap"),
    path("", include("apps.web.urls")),
]

handler404 = "apps.web.views.page_not_found"
