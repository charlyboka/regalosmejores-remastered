"""``UserQuery`` recording.

Every search a visitor runs is recorded — including the ones that redirect straight to a Topic,
which are otherwise invisible yet are the clearest signal of what people actually want.

Recording is synchronous: one INSERT on the connection the view already holds. It is wrapped so
a tracking failure can never turn a working search into an error page.
"""

from __future__ import annotations

import logging

from django.db import DatabaseError
from django.http import HttpRequest

from apps.search.models import SearchMode, UserQuery
from apps.tracking.session import visitor_hash

logger = logging.getLogger(__name__)


def record_query(
    request: HttpRequest,
    *,
    raw_text: str,
    normalized: str,
    query_hash: str = "",
    mode: str = SearchMode.SIMPLE,
    slots: dict | None = None,
    matched_topic=None,
    match_similarity: float | None = None,
    result_count: int = 0,
    served_from_cache: bool = False,
    latency_ms: int | None = None,
) -> UserQuery | None:
    """Write one row. Returns it, or ``None`` if recording failed."""
    from apps.web.views import _host  # local import: avoids an app-loading cycle

    try:
        return UserQuery.objects.create(
            raw_text=raw_text[:500],
            normalized=normalized[:255],
            query_hash=query_hash,
            mode=mode,
            slots=slots or {},
            matched_topic=matched_topic,
            match_similarity=match_similarity,
            result_count=result_count,
            is_zero_result=result_count == 0,
            served_from_cache=served_from_cache,
            latency_ms=latency_ms,
            session_key=visitor_hash(request),
            referrer_host=_host(request.META.get("HTTP_REFERER")),
        )
    except DatabaseError:
        logger.warning("UserQuery not recorded for %r", raw_text[:80], exc_info=True)
    except Exception:
        logger.exception("UserQuery recording failed for %r", raw_text[:80])
    return None
