"""Hybrid retrieval over `ProductFacet` (§6.3 L3).

Three independent searches, fused with Reciprocal Rank Fusion:

- **HNSW cosine** on the facet embedding — catches meaning ("algo para relajarse" → yoga mat).
- **Postgres FTS**, Spanish config — catches exact nouns and brand names, which embeddings blur.
- **pg_trgm** similarity — catches typos and inflections that defeat the stemmer.

RRF is used rather than score blending because the three produce incomparable numbers: a cosine of
0.7, a `ts_rank` of 0.06 and a trigram of 0.4 cannot be averaged. Ranks can.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector, TrigramSimilarity
from django.db.models import F, Q, QuerySet
from pgvector.django import CosineDistance

from apps.catalog.models import EnrichmentStatus, ProductFacet

#: RRF's only knob. 60 is the value from the original paper and is deliberately large: it flattens
#: the contribution of the top ranks so one list cannot dominate the fusion.
RRF_K = 60

#: How deep to go in each individual list before fusing.
PER_LIST_LIMIT = 120

#: Trigram is here for typos deep inside a word, nothing else. It has to be strict: Spanish gift
#: queries share a lot of boilerplate ("regalo para ... que le gusta ..."), so a loose threshold
#: makes every facet look similar to every query. At 0.2 it matched "regalo para alguien que le
#: encanta cocinar" against "regalo para mi hija que duerme con peluches".
TRIGRAM_THRESHOLD = 0.45

#: Below this length a trigram comparison is mostly noise.
TRIGRAM_MIN_CHARS = 8


@dataclass
class Candidate:
    """One product that survived retrieval, with the best facet that found it."""

    product_id: int
    similarity: float = 0.0
    lexical_score: float = 0.0
    rrf_score: float = 0.0
    facet_id: int | None = None
    facet_text: str = ""
    matched_by: set[str] = field(default_factory=set)


def base_facets() -> QuerySet:
    """Hard filters from §6.4. Everything else in retrieval works on top of this."""
    return ProductFacet.objects.filter(
        product__is_active=True,
        product__is_blocked=False,
        product__is_adult=False,
        product__enrichment_status=EnrichmentStatus.DONE,
    )


def retrieve(
    *,
    embedding: list[float],
    normalized: str,
    slots: dict | None = None,
    min_similarity: float = 0.55,
    pool_size: int = 200,
) -> list[Candidate]:
    """Run all three searches and fuse them into a ranked candidate pool."""
    qs = base_facets()
    qs = apply_slot_filters(qs, slots)

    semantic = _semantic(qs, embedding, min_similarity)
    lexical = _lexical(qs, normalized)
    fuzzy = _fuzzy(qs, normalized)

    fused = _fuse(semantic, lexical, fuzzy, pool_size=pool_size)
    return [c for c in fused if _is_relevant(c, min_similarity)]


def _is_relevant(candidate: Candidate, min_similarity: float) -> bool:
    """A candidate must actually match something. This is the guard rail for the whole engine.

    Without it, a product that only tripped the trigram arm arrives with `similarity == 0` and is
    then ranked on quality and freshness alone — so the highest-rated product in the catalogue
    answers every query it has no business answering. An empty result page is recoverable; a
    confident, irrelevant one is not.
    """
    if candidate.similarity >= min_similarity:
        return True
    # A full-text hit is trustworthy on its own: `websearch` requires every term to be present.
    return "lexical" in candidate.matched_by


def apply_slot_filters(qs: QuerySet, slots: dict | None) -> QuerySet:
    """Advanced-search slots are hard filters, not hints (§6.5).

    Tag filters use array overlap against the GIN indexes on the facet. Price band and category
    filter the product. A slot the user did not set is simply absent.
    """
    slots = slots or {}

    for field_name in ("occasions", "recipients", "interests"):
        values = slots.get(field_name)
        if values:
            qs = qs.filter(**{f"{field_name}__overlap": list(values)})

    bands = slots.get("price_bands")
    if bands:
        qs = qs.filter(product__price_band__in=list(bands))

    categories = slots.get("root_category_ids")
    if categories:
        qs = qs.filter(product__root_category_id__in=list(categories))

    return qs


# --- the three searches ------------------------------------------------------


def _semantic(qs: QuerySet, embedding: list[float], min_similarity: float) -> list[tuple]:
    """Cosine ANN over the HNSW index. `1 - distance` is the similarity."""
    max_distance = 1.0 - min_similarity
    rows = (
        qs.filter(embedding__isnull=False)
        .annotate(distance=CosineDistance("embedding", embedding))
        .filter(distance__lte=max_distance)
        .order_by("distance")
        .values_list("id", "product_id", "text", "distance")[:PER_LIST_LIMIT]
    )
    return [(fid, pid, text, 1.0 - float(dist)) for fid, pid, text, dist in rows]


#: Must match the index expression on `ProductFacet` exactly, or Postgres silently sequential-scans.
#: `spanish_unaccent` is created in catalog migration 0005 — queries arrive accent-free, so the
#: index has to be accent-free too.
FTS_CONFIG = "spanish_unaccent"


def _lexical(qs: QuerySet, normalized: str) -> list[tuple]:
    """Spanish full-text search. Matches the GIN index expression exactly, or it won't be used."""
    if not normalized:
        return []
    query = SearchQuery(normalized, config=FTS_CONFIG, search_type="websearch")
    rows = (
        qs.annotate(sv=SearchVector("text", config=FTS_CONFIG))
        .filter(sv=query)
        .annotate(rank=SearchRank(F("sv"), query))
        .order_by("-rank")
        .values_list("id", "product_id", "text", "rank")[:PER_LIST_LIMIT]
    )
    return [(fid, pid, text, float(rank)) for fid, pid, text, rank in rows]


def _fuzzy(qs: QuerySet, normalized: str) -> list[tuple]:
    """Trigram similarity, for typos. Never promotes a candidate on its own — see `_is_relevant`."""
    if len(normalized) < TRIGRAM_MIN_CHARS:
        return []
    rows = (
        qs.annotate(trgm=TrigramSimilarity("text", normalized))
        .filter(trgm__gte=TRIGRAM_THRESHOLD)
        .order_by("-trgm")
        .values_list("id", "product_id", "text", "trgm")[:PER_LIST_LIMIT]
    )
    return [(fid, pid, text, float(score)) for fid, pid, text, score in rows]


# --- fusion ------------------------------------------------------------------


def _fuse(semantic, lexical, fuzzy, *, pool_size: int) -> list[Candidate]:
    """Reciprocal Rank Fusion, collapsed to one entry per product.

    Collapsing here rather than later matters: a product with eight facets would otherwise
    accumulate eight RRF contributions and outrank a better product that matched on one.
    """
    candidates: dict[int, Candidate] = {}
    seen_rank: dict[tuple[str, int], bool] = {}

    for label, rows in (("semantic", semantic), ("lexical", lexical), ("fuzzy", fuzzy)):
        for rank, (facet_id, product_id, text, score) in enumerate(rows):
            cand = candidates.get(product_id)
            if cand is None:
                cand = candidates[product_id] = Candidate(product_id=product_id)

            # Only the product's best-ranked facet in each list contributes, for the reason above.
            if seen_rank.get((label, product_id)):
                continue
            seen_rank[(label, product_id)] = True

            cand.rrf_score += 1.0 / (RRF_K + rank + 1)
            cand.matched_by.add(label)

            if label == "semantic":
                if score > cand.similarity:
                    cand.similarity = score
                    cand.facet_id, cand.facet_text = facet_id, text
            else:
                cand.lexical_score = max(cand.lexical_score, score)
                if not cand.facet_text:
                    cand.facet_id, cand.facet_text = facet_id, text

    ordered = sorted(candidates.values(), key=lambda c: c.rrf_score, reverse=True)
    return ordered[:pool_size]


def facet_embeddings(product_ids: list[int]) -> dict[int, list[float]]:
    """Best-effort representative vector per product, used by the MMR diversity penalty."""
    out: dict[int, list[float]] = {}
    rows = (
        ProductFacet.objects.filter(product_id__in=product_ids, embedding__isnull=False)
        .filter(Q(facet_type="SUMMARY") | Q(weight__gte=1.0))
        .values_list("product_id", "embedding")
    )
    for product_id, embedding in rows:
        out.setdefault(product_id, list(embedding))
    return out
