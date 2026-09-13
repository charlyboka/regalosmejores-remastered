"""The search request pipeline (§6.3).

One entry point, `search()`, used by the web views, the `search` management command and — from
Phase 7 — `curate_topics`. Topic pages and live search must never disagree about what a good
result is, and the only way to guarantee that is one implementation.

No chat-completion call ever happens here. The single paid operation is embedding the query
(~$0.0000002, ~40 ms), and a cache hit avoids even that.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta

from django.utils import timezone

from apps.catalog.models import Product
from apps.clients.llm import LLMClient
from apps.search import normalize, ranking, retrieval
from apps.search.models import RankingConfig, SearchResultCache

logger = logging.getLogger(__name__)

#: Reused across requests on purpose. Building an `LLMClient` opens a fresh TLS connection to
#: OpenAI, which measured 6.9 s cold versus 375 ms warm — a per-process cost, not a per-call one,
#: so a dyno should pay it once rather than on every search.
_client: LLMClient | None = None


def _shared_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


@dataclass
class SearchResponse:
    results: list[ranking.Result] = field(default_factory=list)
    normalized: str = ""
    query_hash: str = ""
    total_candidates: int = 0
    served_from_cache: bool = False
    latency_ms: int = 0
    embed_ms: int = 0

    @property
    def is_zero_result(self) -> bool:
        return not self.results


def search(
    text: str,
    *,
    slots: dict | None = None,
    limit: int | None = None,
    use_cache: bool = True,
    config: RankingConfig | None = None,
    llm: LLMClient | None = None,
) -> SearchResponse:
    started = time.monotonic()
    config = config or RankingConfig.load()
    limit = limit or config.results_per_page

    # L0 — normalise and look in the cache.
    sentence = normalize.slots_to_sentence(slots, text) if slots else text
    normalized = normalize.normalize(sentence)
    query_hash = normalize.cache_key(normalized, slots)

    if use_cache:
        cached = _read_cache(query_hash, limit)
        if cached is not None:
            cached.normalized = normalized
            cached.query_hash = query_hash
            cached.latency_ms = int((time.monotonic() - started) * 1000)
            return cached

    if not normalized:
        return SearchResponse(
            normalized=normalized,
            query_hash=query_hash,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    # L1 — embed. The only paid operation on the request path.
    embed_started = time.monotonic()
    embedding = (llm or _shared_client()).embed_one(normalized, purpose="search")
    embed_ms = int((time.monotonic() - embed_started) * 1000)

    # L3 — hybrid retrieval.
    candidates = retrieval.retrieve(
        embedding=embedding,
        normalized=normalized,
        slots=slots,
        min_similarity=config.min_facet_similarity,
        pool_size=config.candidate_pool_size,
    )

    # L4 — score, then diversify.
    products = _load_products([c.product_id for c in candidates])
    ranked = ranking.rank(candidates, products, config=config)
    embeddings = retrieval.facet_embeddings([r.product.pk for r in ranked[: limit * 4]])
    final = ranking.diversify(ranked, config=config, limit=limit, embeddings=embeddings)

    response = SearchResponse(
        results=final,
        normalized=normalized,
        query_hash=query_hash,
        total_candidates=len(candidates),
        embed_ms=embed_ms,
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    if use_cache:
        _write_cache(query_hash, response, ttl_hours=config.cache_ttl_hours)
    return response


# --- product loading ---------------------------------------------------------


def _load_products(product_ids: list[int]) -> dict[int, Product]:
    if not product_ids:
        return {}
    return Product.objects.in_bulk(product_ids)


# --- cache -------------------------------------------------------------------


def _read_cache(query_hash: str, limit: int) -> SearchResponse | None:
    """Cache stores product ids and the score breakdown; products are re-read live.

    Caching rendered products would serve a deactivated or newly blocked product for up to a day.
    Caching the *ordering* is safe, because the hard filters are re-applied on read.
    """
    row = SearchResultCache.objects.filter(
        query_hash=query_hash, expires_at__gt=timezone.now()
    ).first()
    if row is None:
        return None

    entries = (row.payload or {}).get("results") or []
    products = _load_products([e["product_id"] for e in entries])
    results: list[ranking.Result] = []
    for entry in entries[:limit]:
        product = products.get(entry["product_id"])
        if product is None or not _still_serveable(product):
            continue
        results.append(
            ranking.Result(
                product=product,
                score=entry.get("score", 0.0),
                similarity=entry.get("similarity", 0.0),
                lexical=entry.get("lexical", 0.0),
                quality=product.quality_score,
                ctr=product.ctr_score,
                freshness=entry.get("freshness", 0.0),
                facet_text=entry.get("facet_text", ""),
                matched_by=set(entry.get("matched_by") or []),
            )
        )

    SearchResultCache.objects.filter(pk=row.pk).update(hits=row.hits + 1)
    return SearchResponse(
        results=results,
        total_candidates=(row.payload or {}).get("total_candidates", len(entries)),
        served_from_cache=True,
    )


def _still_serveable(product: Product) -> bool:
    return product.is_active and not product.is_blocked and not product.is_adult


def _write_cache(query_hash: str, response: SearchResponse, *, ttl_hours: int) -> None:
    payload = {
        "total_candidates": response.total_candidates,
        "results": [
            {
                "product_id": r.product.pk,
                "score": round(r.score, 6),
                "similarity": round(r.similarity, 6),
                "lexical": round(r.lexical, 6),
                "freshness": round(r.freshness, 6),
                "facet_text": r.facet_text,
                "matched_by": sorted(r.matched_by),
            }
            for r in response.results
        ],
    }
    SearchResultCache.objects.update_or_create(
        query_hash=query_hash,
        defaults={
            "payload": payload,
            "expires_at": timezone.now() + timedelta(hours=ttl_hours),
            "hits": 0,
        },
    )
