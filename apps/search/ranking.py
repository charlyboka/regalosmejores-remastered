"""Scoring and diversity (§6.3 L4). Pure SQL and Python — no model call on the request path."""

from __future__ import annotations

import math
from dataclasses import dataclass

from django.utils import timezone

from apps.catalog.models import Product

#: Freshness decays from 1.0 at fetch time to this floor. Never zero: a product we have not
#: re-fetched recently is stale, not wrong, and burying it entirely would empty thin categories.
FRESHNESS_FLOOR = 0.6
FRESHNESS_HALFLIFE_DAYS = 90

#: RRF scores are tiny (~0.016 at rank 1) and unbounded below, so they are rescaled against the
#: best score in the pool before being weighted. Otherwise `w_lexical` would do nothing.
_EPSILON = 1e-9


@dataclass
class Result:
    """One ranked product, with the score breakdown that makes tuning possible."""

    product: Product
    score: float
    similarity: float
    lexical: float
    quality: float
    ctr: float
    freshness: float
    facet_text: str
    matched_by: set[str]

    def explain(self) -> str:
        return (
            f"sim={self.similarity:.3f} lex={self.lexical:.3f} qual={self.quality:.3f} "
            f"ctr={self.ctr:.3f} fresh={self.freshness:.3f} -> {self.score:.4f}"
        )


def rank(candidates, products: dict[int, Product], *, config) -> list[Result]:
    """Weighted linear score. Weights live in `RankingConfig`, tunable in Admin without a deploy."""
    if not candidates:
        return []

    best_rrf = max((c.rrf_score for c in candidates), default=0.0) or _EPSILON
    now = timezone.now()
    results: list[Result] = []

    for cand in candidates:
        product = products.get(cand.product_id)
        if product is None:
            continue

        lexical = cand.rrf_score / best_rrf
        freshness = _freshness(product.keepa_fetched_at, now)
        score = (
            config.w_semantic * cand.similarity
            + config.w_lexical * lexical
            + config.w_quality * product.quality_score
            + config.w_ctr * product.ctr_score
            + config.w_freshness * freshness
        )
        results.append(
            Result(
                product=product,
                score=score,
                similarity=cand.similarity,
                lexical=lexical,
                quality=product.quality_score,
                ctr=product.ctr_score,
                freshness=freshness,
                facet_text=cand.facet_text,
                matched_by=cand.matched_by,
            )
        )

    results.sort(key=lambda r: r.score, reverse=True)
    return results


def _freshness(fetched_at, now) -> float:
    if fetched_at is None:
        return FRESHNESS_FLOOR
    days = max((now - fetched_at).days, 0)
    decay = math.exp(-days / FRESHNESS_HALFLIFE_DAYS)
    return FRESHNESS_FLOOR + (1.0 - FRESHNESS_FLOOR) * decay


def diversify(results: list[Result], *, config, limit: int, embeddings=None) -> list[Result]:
    """Greedy diversity pass in rank order (§6.4).

    Rule 1 is the one that matters. Amazon sells the same craft kit in eleven colour variants, all
    with near-identical facets, so without collapsing on `variation_group_key` the first page is
    one product eleven times. Everything else is polish; this is the difference between a grid
    that looks curated and one that looks broken.
    """
    embeddings = embeddings or {}
    selected: list[Result] = []
    seen_variations: set[str] = set()
    per_brand: dict[str, int] = {}
    per_category: dict[int, int] = {}
    per_band: dict[str, int] = {}
    band_cap = max(1, math.ceil(limit / 2))

    # Brand stays absolute: two Bosch drills on a page is two too many regardless of page size.
    # Category scales, because the category usually *is* the query — capping "juguetes" at 3 on a
    # 24-result page would answer "regalo para un niño" with 3 toys and 21 unrelated products. The
    # Admin value acts as the floor, so small pages keep the tighter spread.
    brand_cap = config.max_per_brand
    category_cap = max(config.max_per_category, math.ceil(limit / 3))

    for result in results:
        if len(selected) >= limit:
            break
        product = result.product

        variation = product.variation_group_key or product.asin
        if variation in seen_variations:
            continue

        brand = (product.brand or "").lower()
        if brand and per_brand.get(brand, 0) >= brand_cap:
            continue

        category = product.root_category_id
        if category and per_category.get(category, 0) >= category_cap:
            continue

        # Soft price spread: stop any single band from taking more than half the page, but only
        # while alternatives exist. Thin result sets keep everything.
        band = product.price_band or ""
        if band and per_band.get(band, 0) >= band_cap and len(results) > limit * 2:
            continue

        if _too_similar(result, selected, embeddings):
            continue

        selected.append(result)
        seen_variations.add(variation)
        if brand:
            per_brand[brand] = per_brand.get(brand, 0) + 1
        if category:
            per_category[category] = per_category.get(category, 0) + 1
        if band:
            per_band[band] = per_band.get(band, 0) + 1

    # Never return a short page just because the rules were strict. Backfill with whatever was
    # rejected, still in score order, but never breaking rule 1.
    if len(selected) < limit:
        for result in results:
            if len(selected) >= limit:
                break
            variation = result.product.variation_group_key or result.product.asin
            if variation in seen_variations:
                continue
            selected.append(result)
            seen_variations.add(variation)

    return selected


#: Cosine above which two selected products are considered redundant (the MMR penalty).
MMR_THRESHOLD = 0.93


def _too_similar(result: Result, selected: list[Result], embeddings: dict) -> bool:
    vector = embeddings.get(result.product.pk)
    if not vector:
        return False
    for chosen in selected:
        other = embeddings.get(chosen.product.pk)
        if other and _cosine(vector, other) >= MMR_THRESHOLD:
            return True
    return False


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
