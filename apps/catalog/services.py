"""Keepa → `Product` mapping and the derived fields that ingestion computes.

Kept out of the pipelines so that `hydrate_products` and `refresh_products` cannot drift apart:
there is exactly one function that turns Keepa data into a Product row.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from django.utils import timezone

from apps.catalog import categories
from apps.catalog.models import EnrichmentStatus, PriceBand, Product

#: Review count at which the social-proof term of the quality score saturates.
REVIEW_SATURATION = 5000

#: Used when a product has no sales rank. Neutral rather than worst: a missing rank is usually a
#: gap in Keepa's stats window, not evidence that the product sells badly.
NEUTRAL_SALES_RANK_PCT = 0.5


@dataclass(frozen=True, slots=True)
class QualityGate:
    """The same standards applied at seeding time, re-applied after hydration.

    Seeding filters are enforced by Keepa's Product Finder, but a product can drift below them
    between harvest and hydration, and `refresh_products` re-checks them for life. Raising these
    later cleans the catalogue retroactively on the next refresh.
    """

    min_rating: float = 4.2
    min_reviews: int = 150
    min_price_cents: int = 1500
    require_images: bool = True
    require_brand: bool = True

    def evaluate(self, product: Product) -> str:
        """Return an empty string when the product passes, or a Spanish reason when it does not."""
        if product.is_adult:
            return "Producto para adultos"
        if not categories.is_ingestable(product.root_category_id):
            name = categories.name_for(product.root_category_id) or product.root_category_id
            return f"Categoría fuera de alcance: {name}"
        if (product.product_group or "").strip().lower() in categories.BLOCKED_PRODUCT_GROUPS:
            return f"Tipo de producto excluido: {product.product_group}"
        if (product.binding or "").strip().lower() in categories.BLOCKED_BINDINGS:
            return f"Formato excluido: {product.binding}"
        if self.require_images and not product.image_urls:
            return "Sin imágenes"
        if self.require_brand and not product.brand:
            return "Sin marca"
        if product.rating is None or float(product.rating) < self.min_rating:
            return f"Valoración insuficiente: {product.rating}"
        if (product.review_count or 0) < self.min_reviews:
            return f"Pocas reseñas: {product.review_count or 0}"
        # A €7 pack of batteries reads as a thoughtless gift and earns a commission that is not
        # worth the slot on the page. Cheap items are filtered at serving time, not just at
        # harvest time, so raising this threshold later cleans the catalogue on the next pass.
        if product.price_cents is not None and product.price_cents < self.min_price_cents:
            return f"Precio por debajo del mínimo: {product.price_cents / 100:.2f} €"
        return ""


def compute_sales_rank_pct(sales_rank: int | None, root_category_id: int | None) -> float | None:
    """0 = best seller in its root category, 1 = worst.

    Divided by the category's real product count, so rank 1 000 scores very differently in
    Videojuegos (457 k products) than in Hogar y cocina (51 M).
    """
    if not sales_rank or sales_rank <= 0:
        return None
    total = categories.product_count_for(root_category_id)
    return min(sales_rank / total, 1.0)


def compute_quality_score(
    *, rating: float | None, review_count: int | None, sales_rank_pct: float | None
) -> float:
    """Blueprint §6.4. Bounded to [0, 1]."""
    rating_term = (float(rating) / 5.0) if rating else 0.0
    reviews_term = math.log1p(review_count or 0) / math.log1p(REVIEW_SATURATION)
    rank_pct = NEUTRAL_SALES_RANK_PCT if sales_rank_pct is None else sales_rank_pct
    score = 0.5 * rating_term + 0.3 * min(reviews_term, 1.0) + 0.2 * (1.0 - rank_pct)
    return round(min(max(score, 0.0), 1.0), 6)


def compute_price_band(
    price_cents: int | None, *, economico_max: int, medio_max: int
) -> str | None:
    if not price_cents or price_cents <= 0:
        return None
    if price_cents <= economico_max:
        return PriceBand.ECONOMICO
    if price_cents <= medio_max:
        return PriceBand.MEDIO
    return PriceBand.PREMIUM


def apply_keepa_product(product: Product, kp, *, gate: QualityGate, config) -> Product:
    """Copy a `KeepaProduct` onto a `Product` row and recompute everything derived.

    Does not save — the caller batches. Returns the same instance for convenience.
    """
    product.title = kp.title
    product.brand = kp.brand
    product.manufacturer = kp.manufacturer
    product.model = kp.model
    product.parent_asin = kp.parent_asin
    product.product_group = kp.product_group
    product.binding = kp.binding
    product.root_category_id = kp.root_category_id
    product.category_ids = kp.category_ids
    product.features = kp.features
    product.description_text = kp.description_text
    product.image_urls = kp.image_urls
    product.rating = kp.rating
    product.review_count = kp.review_count
    product.sales_rank = kp.sales_rank
    product.is_adult = kp.is_adult
    product.listed_since = kp.listed_since
    product.variation_group_key = kp.variation_group_key or kp.asin
    product.price_cents = kp.price_cents
    product.keepa_payload_hash = kp.payload_hash
    product.keepa_fetched_at = timezone.now()
    product.is_active = True

    recompute_derived(product, config=config)
    apply_gate(product, gate)
    return product


def recompute_derived(product: Product, *, config) -> Product:
    """Recompute price band, rank percentile and quality score from already-stored fields.

    Pure DB work — no API calls — so it is safe to run over the whole catalogue whenever the
    band thresholds or the scoring weights change.
    """
    previous_band = product.price_band
    product.price_band = compute_price_band(
        product.price_cents,
        economico_max=config.price_band_economico_max_cents,
        medio_max=config.price_band_medio_max_cents,
    )
    if product.price_band != previous_band or product.price_band_at is None:
        product.price_band_at = timezone.now()

    product.sales_rank_pct = compute_sales_rank_pct(product.sales_rank, product.root_category_id)
    product.quality_score = compute_quality_score(
        rating=float(product.rating) if product.rating is not None else None,
        review_count=product.review_count,
        sales_rank_pct=product.sales_rank_pct,
    )
    return product


def apply_gate(product: Product, gate: QualityGate) -> None:
    """Park products that fail the standards so we never spend LLM budget on them.

    SKIPPED (not `is_active=False`) is the right state: the product genuinely is available on
    Amazon, it just isn't something we want to recommend. `is_active` stays reserved for
    "Keepa no longer returns this ASIN".
    """
    reason = gate.evaluate(product)
    if reason:
        if product.enrichment_status != EnrichmentStatus.DONE:
            product.enrichment_status = EnrichmentStatus.SKIPPED
        product.availability_note = reason[:255]
    elif product.enrichment_status == EnrichmentStatus.SKIPPED:
        # It failed before and passes now — let it back into the enrichment queue.
        product.enrichment_status = EnrichmentStatus.PENDING
        product.availability_note = ""
    elif product.availability_note:
        product.availability_note = ""


#: Fields `apply_keepa_product` touches. Passed to `bulk_update` so we never write whole rows.
HYDRATION_FIELDS = [
    "title",
    "brand",
    "manufacturer",
    "model",
    "parent_asin",
    "product_group",
    "binding",
    "root_category_id",
    "category_ids",
    "features",
    "description_text",
    "image_urls",
    "rating",
    "review_count",
    "sales_rank",
    "is_adult",
    "listed_since",
    "variation_group_key",
    "price_cents",
    "price_band",
    "price_band_at",
    "sales_rank_pct",
    "quality_score",
    "keepa_payload_hash",
    "keepa_fetched_at",
    "is_active",
    "availability_note",
    "enrichment_status",
    "updated_at",
]
