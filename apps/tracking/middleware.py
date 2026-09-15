"""``PageView`` recording.

Runs after the response is produced so a tracking failure can never break a page, and only for
successful HTML GETs. Bots are recorded but flagged, so the dashboard can exclude them without
losing the ability to see how much crawler traffic the site is getting.
"""

from __future__ import annotations

import logging

from django.db import DatabaseError
from django.http import HttpRequest, HttpResponse

from apps.tracking.models import PageView
from apps.tracking.session import is_bot, visitor_hash

logger = logging.getLogger(__name__)

# Nothing here is a page a human "visited".
IGNORED_PREFIXES = ("/admin/", "/static/", "/media/", "/go/", "/healthz", "/__")
IGNORED_PATHS = ("/robots.txt", "/sitemap.xml", "/favicon.ico")


class PageViewMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        try:
            if self._should_record(request, response):
                self._record(request, response)
        except DatabaseError:
            # The page is already rendered; losing one analytics row is not worth a 500.
            logger.warning("PageView not recorded for %s", request.path, exc_info=True)
        except Exception:
            logger.exception("PageView middleware failed for %s", request.path)
        return response

    @staticmethod
    def _should_record(request: HttpRequest, response: HttpResponse) -> bool:
        if request.method != "GET" or response.status_code != 200:
            return False
        if request.headers.get("HX-Request"):
            return False  # A partial refresh is not a new page view.
        if not response.get("Content-Type", "").startswith("text/html"):
            return False
        path = request.path
        return not (path in IGNORED_PATHS or path.startswith(IGNORED_PREFIXES))

    @staticmethod
    def _record(request: HttpRequest, response: HttpResponse) -> None:
        from apps.web.views import _host  # local import: avoids an app-loading cycle

        PageView.objects.create(
            path=request.path[:255],
            # Set by the topic view so the hot path here stays free of extra queries.
            topic_id=getattr(request, "page_topic_id", None),
            session_key=visitor_hash(request),
            referrer_host=_host(request.META.get("HTTP_REFERER")),
            is_bot=is_bot(request),
        )
