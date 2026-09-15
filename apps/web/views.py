"""Public site views. Blueprint §8.

The routing rule that shapes this whole module: **the long tail is never indexed.** Search
results are machine-made and unbounded, so `/buscar/` is always `noindex` and, when a query
matches a curated Topic strongly, it 302s to that Topic instead. All SEO value concentrates on a
finite, quality-gated set of pages rather than leaking into thousands of thin ones.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from apps.catalog import vocabularies
from apps.catalog.models import FacetType, PriceBand, Product, ProductFacet
from apps.search import engine, recording
from apps.search.models import SearchMode, UserQuery
from apps.search.normalize import normalize, slots_to_sentence
from apps.topics import services as topic_services
from apps.topics.models import Topic, TopicKind, TopicStatus
from apps.tracking.models import ClickButton, ClickEvent, Placement
from apps.tracking.session import visitor_hash

from . import affiliate, seo

logger = logging.getLogger(__name__)

#: Products per page everywhere. Divisible by 2, 3 and 4 so the grid never leaves a ragged row.
PAGE_SIZE = 24

#: How many sibling topics to link from a topic page (§8.4 internal linking).
RELATED_TOPICS = 6

#: The four hubs, in URL order. Each maps to the ``TopicKind`` it lists.
HUBS: dict[str, tuple[str, str, str]] = {
    "ocasiones": (
        TopicKind.OCCASION,
        "Regalos por ocasión",
        "Encuentra el regalo adecuado para cada celebración.",
    ),
    "para-quien": (
        TopicKind.RECIPIENT,
        "Regalos para cada persona",
        "Ideas ordenadas por quién va a recibirlas.",
    ),
    "aficiones": (
        TopicKind.INTEREST,
        "Regalos por afición",
        "Para quien ya tiene un hobby y lo disfruta de verdad.",
    ),
    "presupuesto": (
        TopicKind.BUDGET,
        "Regalos por presupuesto",
        "Ideas agrupadas por lo que quieres gastarte.",
    ),
}

#: ``slug -> (title, indexable)``. Only the affiliate disclosure is worth indexing.
LEGAL_PAGES: dict[str, tuple[str, bool]] = {
    "aviso-legal": ("Aviso legal", False),
    "privacidad": ("Política de privacidad", False),
    "cookies": ("Política de cookies", False),
    "afiliados": ("Divulgación de afiliación", True),
    "contacto": ("Contacto", False),
}


@never_cache
def healthz(request: HttpRequest) -> JsonResponse:
    """Liveness probe used by the Heroku deploy health check.

    Deliberately does not touch the database: it must answer even when Postgres is
    unreachable, otherwise a DB blip would roll back an otherwise healthy release.
    """
    return JsonResponse({"status": "ok"})


# --- pages ---------------------------------------------------------------------------------


@require_GET
def home(request: HttpRequest) -> HttpResponse:
    featured = list(
        Topic.objects.filter(
            status=TopicStatus.ACTIVE, is_indexable=True, merged_into__isnull=True
        ).order_by("-priority", "-quality_score")[:8]
    )
    return render(
        request,
        "web/home.html",
        {
            "featured": featured,
            "seasonal": topic_services.seasonal_topics(limit=4),
            "hubs": [(slug, meta[1]) for slug, meta in HUBS.items()],
            "placement": Placement.HOME,
            "jsonld": seo.jsonld(
                seo.website("Regalos Mejores", settings.SITE_URL, reverse("web:search"))
            ),
        },
    )


@require_GET
def topic_detail(request: HttpRequest, slug: str) -> HttpResponse:
    topic = Topic.objects.filter(slug=slug).select_related("merged_into").first()
    if topic is None:
        target = topic_services.topic_for_alias_slug(slug)
        if target is None:
            return _not_found(request)
        return redirect(target.get_absolute_url(), permanent=True)

    # A merged topic is a page that may already rank. Keep the equity: 301, never 404.
    if topic.merged_into_id:
        return redirect(topic.merged_into.get_absolute_url(), permanent=True)

    if topic.status == TopicStatus.ARCHIVED:
        return _not_found(request)

    # Read by the PageView middleware, so it can attribute the visit without a second lookup.
    request.page_topic_id = topic.pk

    links = topic_services.visible_links(topic)[: PAGE_SIZE + 1]
    shown = [link.product for link in links[:PAGE_SIZE]]
    with_summaries(shown)
    page_url = settings.SITE_URL + topic.get_absolute_url()
    return render(
        request,
        "web/topic.html",
        {
            "topic": topic,
            "links": links[:PAGE_SIZE],
            "has_more": len(links) > PAGE_SIZE,
            "next_offset": PAGE_SIZE,
            "start_position": 1,
            "related": topic_services.related_topics(topic, limit=RELATED_TOPICS),
            "chips": _topic_chips(topic),
            "placement": Placement.TOPIC_PAGE,
            "jsonld_list": seo.jsonld(seo.item_list(topic.title, page_url, shown)),
            "jsonld_crumbs": seo.jsonld(
                seo.breadcrumbs(
                    [
                        ("Inicio", settings.SITE_URL + reverse("web:home")),
                        (topic.title, page_url),
                    ]
                )
            ),
        },
    )


@require_GET
def topic_more(request: HttpRequest, slug: str) -> HttpResponse:
    """HTMX "cargar más". Returns card markup only, never a full document."""
    topic = get_object_or_404(Topic.objects.filter(merged_into__isnull=True), slug=slug)
    offset = _bounded_int(request.GET.get("offset"), default=PAGE_SIZE, maximum=500)
    links = topic_services.visible_links(topic)[offset : offset + PAGE_SIZE + 1]
    with_summaries([link.product for link in links[:PAGE_SIZE]])
    return render(
        request,
        "web/partials/topic_products.html",
        {
            "topic": topic,
            "links": links[:PAGE_SIZE],
            "has_more": len(links) > PAGE_SIZE,
            "next_offset": offset + PAGE_SIZE,
            "start_position": offset + 1,
            "placement": Placement.TOPIC_PAGE,
        },
    )


@require_GET
def hub(request: HttpRequest, hub_slug: str) -> HttpResponse:
    if hub_slug not in HUBS:
        return _not_found(request)
    kind, title, intro = HUBS[hub_slug]
    topics = list(
        Topic.objects.filter(
            kind=kind, status=TopicStatus.ACTIVE, is_indexable=True, merged_into__isnull=True
        ).order_by("-priority", "title")
    )
    return render(
        request,
        "web/hub.html",
        {
            "hub_slug": hub_slug,
            "hub_title": title,
            "hub_intro": intro,
            "topics": topics,
            "jsonld_crumbs": seo.jsonld(
                seo.breadcrumbs(
                    [
                        ("Inicio", settings.SITE_URL + reverse("web:home")),
                        (title, settings.SITE_URL + request.path),
                    ]
                )
            ),
        },
    )


@require_GET
def buscar(request: HttpRequest) -> HttpResponse:
    """Simple search. Always ``noindex``; redirects to a Topic when one clearly matches."""
    raw = (request.GET.get("q") or "").strip()
    page = _bounded_int(request.GET.get("page"), default=1, maximum=10) or 1

    if not raw:
        return render(request, "web/search.html", {"query": "", "results": []})

    # Only on the first page: a strong Topic match means a curated, indexable page already
    # exists for this intent. Send the user there rather than serving a thin duplicate of it.
    if page == 1:
        normalized = normalize(raw)
        match = topic_services.match_topic(normalized)
        if match is not None:
            # Recorded before redirecting: these are the clearest demand signal we get, and
            # they would otherwise never appear anywhere.
            recording.record_query(
                request,
                raw_text=raw,
                normalized=normalized,
                matched_topic=match,
                result_count=1,
            )
            return redirect(match.get_absolute_url())

    response = engine.search(raw, limit=PAGE_SIZE * page + 1)
    results = response.results[PAGE_SIZE * (page - 1) : PAGE_SIZE * page]
    with_summaries([r.product for r in results])
    user_query = recording.record_query(
        request,
        raw_text=raw,
        normalized=response.normalized,
        query_hash=response.query_hash,
        result_count=len(response.results),
        served_from_cache=response.served_from_cache,
        latency_ms=response.latency_ms,
    )
    return render(
        request,
        "web/search.html",
        {
            "query": raw,
            "response": response,
            "results": results,
            "start_position": PAGE_SIZE * (page - 1) + 1,
            "has_more": len(response.results) > PAGE_SIZE * page,
            "next_page": page + 1,
            "placement": Placement.SIMPLE_SEARCH,
            "user_query_id": user_query.pk if user_query else None,
            "suggestions": [] if results else topic_services.suggestions(limit=6),
        },
    )


@require_GET
def advanced_search(request: HttpRequest) -> HttpResponse:
    """The advanced search *tool* page. Indexable: it is a real utility, not a results page."""
    return render(
        request,
        "web/advanced.html",
        {
            "occasions": list(vocabularies.OCCASIONS.items()),
            "recipients": list(vocabularies.RECIPIENTS.items()),
            "interests": list(vocabularies.INTERESTS.items()),
            "price_bands": PriceBand.choices,
            "selected": request.session.get("advanced_slots") or {},
            "free_text": request.session.get("advanced_text", ""),
        },
    )


@require_POST
def advanced_search_submit(request: HttpRequest) -> HttpResponse:
    """Post/Redirect/Get, so a reload never resubmits and the results URL stays clean."""
    slots: dict[str, list[str]] = {
        "occasions": _clean_multi(request, "occasions", vocabularies.OCCASIONS),
        "recipients": _clean_multi(request, "recipients", vocabularies.RECIPIENTS),
        "interests": _clean_multi(request, "interests", vocabularies.INTERESTS),
    }
    band = (request.POST.get("price_band") or "").strip()
    if band in PriceBand.values:
        slots["price_bands"] = [band]

    request.session["advanced_slots"] = {k: v for k, v in slots.items() if v}
    request.session["advanced_text"] = (request.POST.get("q") or "").strip()[:200]
    return redirect("web:advanced_results")


@require_GET
def advanced_results(request: HttpRequest) -> HttpResponse:
    slots = request.session.get("advanced_slots") or {}
    free_text = request.session.get("advanced_text", "")
    if not slots and not free_text:
        return redirect("web:advanced_search")

    # §6.5: the sentence is composed from vocabulary labels. Still zero LLM calls.
    sentence = slots_to_sentence(slots, free_text)
    response = engine.search(sentence, slots=slots, limit=PAGE_SIZE)
    with_summaries([r.product for r in response.results])
    user_query = recording.record_query(
        request,
        raw_text=free_text or sentence,
        normalized=response.normalized,
        query_hash=response.query_hash,
        mode=SearchMode.ADVANCED,
        slots=slots,
        result_count=len(response.results),
        served_from_cache=response.served_from_cache,
        latency_ms=response.latency_ms,
    )
    return render(
        request,
        "web/advanced_results.html",
        {
            "sentence": sentence,
            "chips": _slot_chips(slots),
            "response": response,
            "results": response.results,
            "start_position": 1,
            "placement": Placement.ADVANCED_SEARCH,
            "user_query_id": user_query.pk if user_query else None,
            "suggestions": [] if response.results else topic_services.suggestions(limit=6),
        },
    )


@require_GET
def legal(request: HttpRequest, page: str) -> HttpResponse:
    if page not in LEGAL_PAGES:
        return _not_found(request)
    title, indexable = LEGAL_PAGES[page]
    return render(
        request,
        f"web/legal/{page.replace('-', '_')}.html",
        {"page_title": title, "legal_indexable": indexable},
    )


# --- outbound ------------------------------------------------------------------------------


@never_cache
@require_GET
def go(request: HttpRequest, product_id: int) -> HttpResponse:
    """Record the click, then 302 to the tagged Amazon URL. Blueprint §8.3.

    The blueprint writes this route as ``/go/<click_id>/``, which cannot work literally: the
    ``ClickEvent`` does not exist until someone clicks, so its id cannot already be in the
    rendered href. The product id goes in the path, the event is created here, and the click id
    it receives is what lands in the ``ascsubtag``.

    There is no open-redirect risk to guard against: the destination is assembled from settings
    and the product's own ASIN, never from anything in the request.
    """
    product = get_object_or_404(
        Product.objects.filter(is_active=True, is_blocked=False), pk=product_id
    )

    button = _choice(request.GET.get("b"), ClickButton, ClickButton.DETAILS)
    placement = _choice(request.GET.get("p"), Placement, Placement.TOPIC_PAGE)
    topic_id = _bounded_int(request.GET.get("t"), default=0, maximum=2_000_000_000) or None
    if topic_id and not Topic.objects.filter(pk=topic_id).exists():
        topic_id = None

    # Which search produced this click, when it came from a results page. Validated because it
    # arrives from the query string; an unknown id is simply dropped.
    query_id = _bounded_int(request.GET.get("uq"), default=0, maximum=2_000_000_000) or None
    if query_id and not UserQuery.objects.filter(pk=query_id).exists():
        query_id = None

    click = ClickEvent.objects.create(
        product=product,
        topic_id=topic_id,
        user_query_id=query_id,
        placement=placement,
        button=button,
        position=_bounded_int(request.GET.get("pos"), default=0, maximum=500) or None,
        page_path=_path(request.META.get("HTTP_REFERER")),
        referrer_host=_host(request.META.get("HTTP_REFERER")),
        session_key=visitor_hash(request),
    )

    subtag = affiliate.build_ascsubtag(placement=placement, topic_id=topic_id, click_id=click.pk)
    ClickEvent.objects.filter(pk=click.pk).update(ascsubtag=subtag)

    return HttpResponseRedirect(
        affiliate.build_target_url(product, button=button, ascsubtag=subtag)
    )


@require_GET
def robots_txt(request: HttpRequest) -> HttpResponse:
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /go/",
        "Disallow: /buscar/",
        "Disallow: /buscador-avanzado/resultados/",
        "Disallow: /admin/",
        "Disallow: /healthz/",
        "",
        f"Sitemap: {settings.SITE_URL}{reverse('sitemap')}",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain; charset=utf-8")


# --- helpers -------------------------------------------------------------------------------


def _not_found(request: HttpRequest) -> HttpResponse:
    return render(request, "web/404.html", status=404)


def with_summaries(products: list[Product]) -> list[Product]:
    """Bulk-load the one-liner each card shows. One query for the whole page, never per card."""
    products = [p for p in products if p is not None]
    if not products:
        return products
    texts: dict[int, str] = {}
    rows = (
        ProductFacet.objects.filter(
            product_id__in=[p.pk for p in products], facet_type=FacetType.SUMMARY
        )
        .order_by("product_id", "-weight")
        .values_list("product_id", "text")
    )
    for product_id, text in rows:
        texts.setdefault(product_id, text)
    for product in products:
        product._gift_summary = texts.get(product.pk, "")  # noqa: SLF001
    return products


def page_not_found(request: HttpRequest, exception=None) -> HttpResponse:
    """Project-wide 404 handler, so a wrong URL still offers somewhere to go."""
    return _not_found(request)


def _bounded_int(raw: object, *, default: int, maximum: int) -> int:
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return default
    return max(0, min(value, maximum))


def _choice(raw: object, choices, fallback: str) -> str:
    value = str(raw or "").upper()
    return value if value in choices.values else fallback


def _clean_multi(request: HttpRequest, field: str, allowed: dict[str, str]) -> list[str]:
    return [v for v in request.POST.getlist(field) if v in allowed][:6]


def _path(url: str | None) -> str:
    return (urlparse(url).path if url else "")[:255]


def _host(url: str | None) -> str:
    return ((urlparse(url).hostname or "") if url else "")[:255]


def _topic_chips(topic: Topic) -> list[str]:
    keys = list(topic.recipients) + list(topic.occasions) + list(topic.interests)
    return [vocabularies.label(k) for k in keys][:8]


def _slot_chips(slots: dict) -> list[str]:
    chips = [
        vocabularies.label(key)
        for field in ("recipients", "occasions", "interests")
        for key in slots.get(field, [])
    ]
    return chips + [PriceBand(b).label for b in slots.get("price_bands", [])]
