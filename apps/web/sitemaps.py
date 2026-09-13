"""Sitemaps. Blueprint §8.4 — only pages we are willing to stand behind.

The topic sitemap is filtered by `is_indexable`, which is computed by the §9.2 quality gate. That
is the single mechanism keeping thin, machine-made pages out of Google: a topic that loses its
products drops out of here on the next curation run without anyone deciding anything.
"""

from __future__ import annotations

from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from apps.topics.models import Topic, TopicStatus


class TopicSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.8

    def items(self):
        return Topic.objects.filter(
            status=TopicStatus.ACTIVE, is_indexable=True, merged_into__isnull=True
        ).order_by("slug")

    def lastmod(self, obj: Topic):
        return obj.last_curated_at or obj.updated_at


class StaticSitemap(Sitemap):
    changefreq = "monthly"
    priority = 0.5

    def items(self) -> list[str]:
        return [
            "web:home",
            "web:advanced_search",
            "web:hub_ocasiones",
            "web:hub_para_quien",
            "web:hub_aficiones",
            "web:hub_presupuesto",
            "web:afiliados",
        ]

    def location(self, item: str) -> str:
        return reverse(item)


SITEMAPS = {"topics": TopicSitemap, "static": StaticSitemap}
