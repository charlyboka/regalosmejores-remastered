"""JSON-LD builders. Blueprint §8.4.

Structured data is assembled in Python rather than hand-written in templates for one blunt
reason: a stray quote in a product title silently corrupts the whole block, and Google reports
it as "parsing error" with no indication of which page. Serialising a dict cannot produce
invalid JSON.

We emit `ItemList`, never `Product`. `Product` markup wants `offers.price`, and we deliberately
have no price to give (§1).
"""

from __future__ import annotations

import json
from typing import Any

from django.utils.safestring import SafeString, mark_safe


def jsonld(data: dict[str, Any]) -> SafeString:
    """Serialise for a `<script type="application/ld+json">` block.

    The escaping mirrors Django's own `json_script`: `<`, `>` and `&` are escaped so a title
    containing `</script>` cannot break out of the block and inject markup.
    """
    raw = json.dumps(data, ensure_ascii=False)
    safe = raw.translate({ord("<"): "\\u003c", ord(">"): "\\u003e", ord("&"): "\\u0026"})
    return mark_safe(safe)  # noqa: S308 - escaped above; this is the whole point of the function


def breadcrumbs(trail: list[tuple[str, str]]) -> dict[str, Any]:
    """`trail` is [(name, absolute_url), …] from site root to current page."""
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i, "name": name, "item": url}
            for i, (name, url) in enumerate(trail, start=1)
        ],
    }


def item_list(name: str, url: str, products) -> dict[str, Any]:
    """An ordered list of what the page recommends. No prices, no `Product` nodes."""
    return {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": name,
        "url": url,
        "numberOfItems": len(products),
        "itemListElement": [
            {"@type": "ListItem", "position": i, "name": product.title[:180]}
            for i, product in enumerate(products, start=1)
        ],
    }


def website(site_name: str, site_url: str, search_path: str) -> dict[str, Any]:
    """`WebSite` + `SearchAction`, so Google can offer a sitelinks search box."""
    return {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": site_name,
        "url": site_url,
        "inLanguage": "es-ES",
        "potentialAction": {
            "@type": "SearchAction",
            "target": {
                "@type": "EntryPoint",
                "urlTemplate": f"{site_url}{search_path}?q={{search_term_string}}",
            },
            "query-input": "required name=search_term_string",
        },
    }
