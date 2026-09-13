"""Ingestion: get ASINs into the catalogue and keep them current.

The shape of this phase is **harvest in bulk, hydrate in a trickle**. One Product Finder call
costs ~12 Keepa tokens and returns 50 candidate ASINs; hydrating those same 50 costs ~200. So
seeding is deliberately cheap and frequent, and hydration is the throttled step that the token
budget actually governs.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from apps.catalog import categories
from apps.catalog.models import EnrichmentStatus, Product
from apps.catalog.services import (
    HYDRATION_FIELDS,
    QualityGate,
    apply_gate,
    apply_keepa_product,
    recompute_derived,
)
from apps.clients.keepa import TOKENS_PER_ASIN_ESTIMATE, TOKENS_PER_FINDER_CALL_ESTIMATE
from apps.pipelines.base import Pipeline, PipelineContext, PipelineResult
from apps.pipelines.models import PipelineRun, RunStatus
from apps.pipelines.registry import register
from apps.search.models import RankingConfig

#: Keepa stores ratings as integers ×10. 4.2 stars is 42.
RATING_SCALE = 10


def _gate(ctx: PipelineContext) -> QualityGate:
    return QualityGate(
        min_rating=float(ctx.option("min_rating", 4.2)),
        min_reviews=int(ctx.option("min_reviews", 150)),
        min_price_cents=int(ctx.option("min_price_cents", 1500)),
    )


@register
class SeedProductsPipeline(Pipeline):
    """Harvest candidate ASINs with Keepa's Product Finder and store them unhydrated.

    Rotates across the gift-suitable root categories, preferring whichever is least represented
    in the catalogue, and pages deeper on every visit so it never re-harvests the same top 50.
    """

    key = "seed_products"
    description = "Busca ASINs candidatos con Product Finder y los deja pendientes de hidratar."
    default_cron = "0 */6 * * *"
    default_max_per_run = 50
    priority = 65
    requires_keepa = True
    estimated_keepa_tokens = TOKENS_PER_FINDER_CALL_ESTIMATE * 2
    default_options = {
        "min_rating": 4.2,
        "min_reviews": 150,
        "sales_rank_min": 150,
        "sales_rank_max": 30000,
        "min_price_cents": 1500,
        "max_price_cents": 50000,
        "categories": list(categories.GIFT_SUITABLE_IDS),
    }

    def run(self, ctx: PipelineContext) -> PipelineResult:
        allowed = [int(c) for c in ctx.option("categories", categories.GIFT_SUITABLE_IDS)] or list(
            categories.GIFT_SUITABLE_IDS
        )

        with ctx.step("choose_category") as step:
            cursors = self._load_cursors()
            category_id = ctx.option("category") or self._least_covered(allowed, cursors)
            page = int(cursors.get(str(category_id), 0))
            step.context.update(
                {"category": category_id, "name": categories.name_for(category_id), "page": page}
            )

        selection = self._selection(ctx, category_id=category_id, page=page)

        with ctx.step("product_finder") as step:
            asins, total = ctx.keepa.find_products(selection)
            step.context.update({"returned": len(asins), "total_matching": total})

        with ctx.step("store_candidates") as step:
            created = self._store(asins)
            step.context.update({"created": created, "already_known": len(asins) - created})

        cursors[str(category_id)] = page + 1 if asins else 0

        return PipelineResult(
            items_in=len(asins),
            items_created=created,
            context={
                "category": categories.name_for(category_id) or category_id,
                "page": page,
                "total_matching": total,
                "cursors": cursors,
            },
        )

    def _selection(self, ctx: PipelineContext, *, category_id: int, page: int) -> dict:
        """Build the Product Finder query. Blueprint §7.4.

        Two filters here are doing more work than they look like they are:

        ``sales_rank_min`` skips the very top of every category. Ranks 1-100 are consumables and
        first-party hardware — AA batteries, printer ink, Echo Dots. They sell enormously and make
        terrible gifts. Starting below them lands in the "popular but considered purchase" band.

        ``min_price_cents`` does the same job from the money side: a €7 pack of batteries earns a
        commission not worth the page slot, and cheap items read as thoughtless gifts.
        """
        return {
            "productType": [0],
            "rootCategory": category_id,
            "current_RATING_gte": int(float(ctx.option("min_rating", 4.2)) * RATING_SCALE),
            "current_COUNT_REVIEWS_gte": int(ctx.option("min_reviews", 150)),
            "current_SALES_gte": int(ctx.option("sales_rank_min", 150)),
            "current_SALES_lte": int(ctx.option("sales_rank_max", 30000)),
            "current_NEW_gte": int(ctx.option("min_price_cents", 1500)),
            "current_NEW_lte": int(ctx.option("max_price_cents", 50000)),
            "isAdultProduct": False,
            "sort": [["current_SALES", "asc"]],
            "perPage": max(int(ctx.max_items), 50),
            "page": page,
        }

    @staticmethod
    def _load_cursors() -> dict[str, int]:
        last = (
            PipelineRun.objects.filter(pipeline_key="seed_products", status=RunStatus.SUCCESS)
            .order_by("-started_at")
            .first()
        )
        cursors = (last.context or {}).get("cursors") if last else None
        return dict(cursors) if isinstance(cursors, dict) else {}

    @staticmethod
    def _least_covered(allowed: list[int], cursors: dict[str, int]) -> int:
        """Pick the category with the fewest hydrated products, breaking ties by rotation.

        Before anything is hydrated every count is zero, so without the rotating tie-break the
        first runs would all pile into whichever category happens to have the lowest id.
        """
        counts = dict(
            Product.objects.filter(root_category_id__in=allowed)
            .values_list("root_category_id")
            .order_by()
            .annotate(n=Count("id"))
        )
        run_no = PipelineRun.objects.filter(pipeline_key="seed_products").count()
        order = sorted(allowed)
        size = len(order)
        return min(
            allowed,
            key=lambda cid: (
                counts.get(cid, 0),
                cursors.get(str(cid), 0),
                (order.index(cid) - run_no) % size,
            ),
        )

    @staticmethod
    def _store(asins: list[str]) -> int:
        """Insert stub rows. `keepa_fetched_at` stays NULL, which is what queues them."""
        if not asins:
            return 0
        domain_id = settings.KEEPA_DOMAIN_ID
        known = set(
            Product.objects.filter(asin__in=asins, domain_id=domain_id).values_list(
                "asin", flat=True
            )
        )
        stubs = [
            Product(asin=asin, domain_id=domain_id, title="", variation_group_key=asin)
            for asin in dict.fromkeys(asins)
            if asin not in known
        ]
        if not stubs:
            return 0
        Product.objects.bulk_create(stubs, ignore_conflicts=True, batch_size=500)
        return len(stubs)


class _HydrationPipeline(Pipeline):
    """Shared machinery for the two pipelines that call Keepa `/product` and write Products."""

    requires_keepa = True

    def select(self, ctx: PipelineContext):  # pragma: no cover - overridden
        raise NotImplementedError

    def run(self, ctx: PipelineContext) -> PipelineResult:
        with ctx.step("select") as step:
            products = list(self.select(ctx)[: ctx.max_items])
            step.context["selected"] = len(products)

        if not products:
            return PipelineResult(context={"note": "nada pendiente"})

        by_asin = {p.asin: p for p in products}
        gate = _gate(ctx)
        config = RankingConfig.load()

        with ctx.step("keepa_fetch", asins=len(by_asin)) as step:
            fetched = ctx.keepa.get_products(list(by_asin))
            step.context["fetched"] = len(fetched)

        with ctx.step("write") as step:
            updated, skipped = self._write(fetched, by_asin, gate=gate, config=config)
            missing = self._deactivate_missing(by_asin, {kp.asin for kp in fetched})
            step.context.update({"updated": updated, "skipped": skipped, "missing": missing})

        return PipelineResult(
            items_in=len(products),
            items_updated=updated,
            items_failed=missing,
            context={"skipped_by_gate": skipped, "not_returned_by_keepa": missing},
        )

    @staticmethod
    def _write(fetched, by_asin, *, gate, config) -> tuple[int, int]:
        touched, skipped = [], 0
        for kp in fetched:
            product = by_asin.get(kp.asin)
            if product is None:
                continue
            apply_keepa_product(product, kp, gate=gate, config=config)
            skipped += int(bool(product.availability_note))
            touched.append(product)

        if touched:
            with transaction.atomic():
                Product.objects.bulk_update(touched, HYDRATION_FIELDS, batch_size=200)
        return len(touched), skipped

    @staticmethod
    def _deactivate_missing(by_asin: dict, returned: set[str]) -> int:
        """An ASIN Keepa will not return is delisted. Deactivate rather than delete.

        Deleting would break `ClickEvent`'s PROTECT foreign key and lose the click history that
        the CTR score is built from.
        """
        missing = [asin for asin in by_asin if asin not in returned]
        if not missing:
            return 0
        Product.objects.filter(asin__in=missing, domain_id=settings.KEEPA_DOMAIN_ID).update(
            is_active=False,
            availability_note="Keepa no devuelve este ASIN",
            keepa_fetched_at=timezone.now(),
        )
        return len(missing)


@register
class HydrateProductsPipeline(_HydrationPipeline):
    """Fill in the ASINs that seeding left as stubs. The main Keepa token consumer."""

    key = "hydrate_products"
    description = "Descarga los datos completos de Keepa para los ASINs pendientes."
    default_cron = "*/5 * * * *"
    default_max_per_run = 25
    priority = 10
    estimated_keepa_tokens = 25 * TOKENS_PER_ASIN_ESTIMATE

    def select(self, ctx: PipelineContext):
        return (
            Product.objects.filter(
                keepa_fetched_at__isnull=True, domain_id=settings.KEEPA_DOMAIN_ID
            )
            .order_by("first_seen_at")
            .only("id", "asin")
        )


@register
class RefreshProductsPipeline(_HydrationPipeline):
    """Re-check products we have not seen in a month: price moves, ratings drift, ASINs die."""

    key = "refresh_products"
    description = "Vuelve a consultar en Keepa los productos con datos antiguos."
    default_cron = "0 */2 * * *"
    default_max_per_run = 25
    priority = 70
    estimated_keepa_tokens = 25 * TOKENS_PER_ASIN_ESTIMATE
    default_options = {"refresh_after_days": 30, "min_rating": 4.2, "min_reviews": 150}

    def select(self, ctx: PipelineContext):
        cutoff = timezone.now() - timedelta(days=int(ctx.option("refresh_after_days", 30)))
        return (
            Product.objects.filter(
                keepa_fetched_at__lt=cutoff,
                is_active=True,
                domain_id=settings.KEEPA_DOMAIN_ID,
            )
            # Products parked by the quality gate stay in the table for the audit trail, but we
            # do not spend 4 Keepa tokens each re-fetching something we will never serve.
            .exclude(enrichment_status=EnrichmentStatus.SKIPPED)
            .order_by("keepa_fetched_at")
            .only("id", "asin")
        )


@register
class RecomputeDerivedPipeline(Pipeline):
    """Re-apply current standards to the whole catalogue. Costs nothing — no API calls.

    This is what closes the loop after you change the price-band thresholds, the rating floor or
    the category allowlist: products that no longer meet the bar are parked as SKIPPED, and
    products that now do are returned to the enrichment queue.
    """

    key = "recompute_derived"
    description = "Recalcula banda de precio, ranking, calidad y vuelve a aplicar los filtros."
    default_cron = "20 1 * * *"
    default_max_per_run = 5000
    priority = 80
    default_options = {"min_rating": 4.2, "min_reviews": 150, "min_price_cents": 1500}

    def run(self, ctx: PipelineContext) -> PipelineResult:
        config = RankingConfig.load()
        gate = _gate(ctx)
        fields = [
            "price_band",
            "price_band_at",
            "sales_rank_pct",
            "quality_score",
            "enrichment_status",
            "availability_note",
            "updated_at",
        ]
        total = 0
        parked = 0

        with ctx.step("recompute") as step:
            queryset = Product.objects.filter(keepa_fetched_at__isnull=False).order_by("id")
            for batch in _batches(queryset.iterator(chunk_size=500), 500):
                for product in batch:
                    recompute_derived(product, config=config)
                    apply_gate(product, gate)
                    parked += int(bool(product.availability_note))
                Product.objects.bulk_update(batch, fields, batch_size=500)
                total += len(batch)
                if total >= ctx.max_items:
                    break
            step.context.update({"recomputed": total, "parked": parked})

        return PipelineResult(
            items_in=total,
            items_updated=total,
            items_failed=parked,
            context={
                "parked_by_gate": parked,
                "economico_max_cents": config.price_band_economico_max_cents,
                "medio_max_cents": config.price_band_medio_max_cents,
                "min_price_cents": gate.min_price_cents,
                "min_rating": gate.min_rating,
            },
        )


def _batches(iterator, size: int):
    batch: list = []
    for item in iterator:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
