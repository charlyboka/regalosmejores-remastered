"""Outbound Amazon links. Blueprint §8.3.

Every link to Amazon is built here and nowhere else. Two reasons that matters:

The Associates tag has to be on *every* outbound URL or the click earns nothing, and a tag
appended by hand in a template is a tag that will eventually be forgotten in one template.

The ``ascsubtag`` is the only way revenue ever gets attributed back to a specific page and
placement. Amazon echoes it into the Associates report, which is what later feeds ``ctr_score``
(§9.4). If it is malformed the click still works and the money is still earned — it just becomes
anonymous, and we lose the feedback loop permanently for that click. So it is built from a
strict, dash-free alphabet: the subtag itself is dash-delimited.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

from django.conf import settings

from apps.catalog.models import Product
from apps.tracking.models import ClickButton

#: Which Amazon page each CTA opens. "Ver precio" deliberately lands on the detail page:
#: we never quote a price ourselves, so the price the user sees is always Amazon's own, live.
BUTTON_PATHS: dict[str, str] = {
    ClickButton.SEE_PRICE: "detail",
    ClickButton.SEE_REVIEWS: "reviews",
    ClickButton.DETAILS: "detail",
    ClickButton.IMAGE: "detail",
    ClickButton.TITLE: "detail",
}

_SUBTAG_SAFE = re.compile(r"[^a-z0-9]+")


def _slug_for_subtag(value: object) -> str:
    """Lowercase, strip everything that is not alphanumeric. Dashes are our delimiter."""
    return _SUBTAG_SAFE.sub("", str(value or "").lower())


def build_ascsubtag(*, placement: str, topic_id: int | None, click_id: int) -> str:
    """``rm-<placement>-<topic_id>-<click_id>``, capped at Amazon's 100-character limit."""
    parts = [
        "rm",
        _slug_for_subtag(placement) or "unknown",
        _slug_for_subtag(topic_id) or "0",
        str(click_id),
    ]
    return "-".join(parts)[:100]


def build_target_url(product: Product, *, button: str, ascsubtag: str) -> str:
    """The final amazon.es URL, tagged and attributed."""
    base = settings.AMAZON_MARKETPLACE_URL.rstrip("/")
    path = (
        product.amazon_reviews_path
        if BUTTON_PATHS.get(button) == "reviews"
        else product.amazon_detail_path
    )
    query = {
        "tag": settings.AMAZON_AFFILIATE_TAG,
        "linkCode": "ll1",
        "language": "es_ES",
    }
    if ascsubtag:
        query["ascsubtag"] = ascsubtag
    return f"{base}{path}?{urlencode(query)}"
