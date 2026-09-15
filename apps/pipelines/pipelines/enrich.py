"""Enrichment: turn hydrated products into retrievable gift queries.

Two OpenAI calls per batch, not per product: one chat completion per product to write the queries
(they need individual attention), then a single batched embeddings call for every facet in the
batch (they do not). At ~$0.001 per product this is the most expensive pipeline in the system, so
it is deliberately the slowest-ticking one and it never re-enriches without being told to.
"""

from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.catalog import enrichment
from apps.catalog.models import EnrichmentStatus, Product, ProductFacet
from apps.clients.exceptions import LLMRequestFailed
from apps.pipelines.base import Pipeline, PipelineContext, PipelineResult
from apps.pipelines.registry import register

#: The embeddings endpoint accepts large batches, but a failure loses the whole batch, so we cap
#: it at a size where a retry is cheap. ~8 facets per product means ~32 products per call.
EMBED_BATCH_SIZE = 256


@register
class EnrichProductsPipeline(Pipeline):
    """Generate `ProductFacet` synthetic queries and embed them.

    §6.1: we never embed a product title. The LLM writes the queries a product answers, we embed
    those, and at request time we compare query to query. This is the biggest quality lever in
    the system, which is why the prompt lives in `apps.catalog.enrichment` and not inline here.
    """

    key = "enrich_products"
    description = "Genera consultas sintéticas con el LLM y las embebe para la búsqueda."
    default_cron = "*/15 * * * *"
    default_max_per_run = 10
    priority = 20
    requires_llm = True
    default_options = {
        "min_queries": enrichment.DEFAULT_MIN_QUERIES,
        "max_queries": enrichment.DEFAULT_MAX_QUERIES,
        # Set to true (usually with a payload, for one run) to re-enrich products that are already
        # DONE. That is the switch to pull after changing the prompt or the vocabularies.
        "reenrich": False,
        # FAILED means the model could not write usable queries for this product twice over.
        # Retrying it automatically every 15 minutes would burn budget forever on the same
        # product, so picking them back up is a deliberate act.
        "retry_failed": False,
    }

    def select(self, ctx: PipelineContext):
        statuses = [EnrichmentStatus.PENDING]
        if ctx.option("retry_failed", False):
            statuses.append(EnrichmentStatus.FAILED)
        if ctx.option("reenrich", False):
            statuses.append(EnrichmentStatus.DONE)

        return (
            Product.objects.filter(
                enrichment_status__in=statuses,
                keepa_fetched_at__isnull=False,
                is_active=True,
                is_blocked=False,
                domain_id=settings.KEEPA_DOMAIN_ID,
            )
            # Best products first: if the budget runs out mid-catalogue, the half we enriched is
            # the half worth showing.
            .order_by("-quality_score", "id")
        )

    def run(self, ctx: PipelineContext) -> PipelineResult:
        min_queries = int(ctx.option("min_queries", enrichment.DEFAULT_MIN_QUERIES))
        max_queries = int(ctx.option("max_queries", enrichment.DEFAULT_MAX_QUERIES))
        system = enrichment.build_system_prompt(min_queries=min_queries, max_queries=max_queries)

        with ctx.step("select") as step:
            products = list(self.select(ctx)[: ctx.max_items])
            step.context["selected"] = len(products)

        if not products:
            return PipelineResult(context={"note": "nada pendiente"})

        with ctx.step("generate", products=len(products)) as step:
            generated, failures, model = self._generate(
                ctx, products, system=system, min_queries=min_queries, max_queries=max_queries
            )
            step.context.update(
                {"ok": len(generated), "failed": len(failures), "reasons": list(failures.values())}
            )

        facet_count = sum(len(specs) for specs in generated.values())
        with ctx.step("embed", facets=facet_count) as step:
            vectors = self._embed(ctx, generated)
            step.context["embedded"] = len(vectors)

        with ctx.step("write") as step:
            written = self._write(generated, vectors, model=model)
            self._mark_failed(failures)
            step.context.update({"products": len(generated), "facets": written})

        return PipelineResult(
            items_in=len(products),
            items_created=written,
            items_updated=len(generated),
            items_failed=len(failures),
            context={"facets_per_product": round(written / len(generated), 1) if generated else 0},
        )

    # --- steps ---------------------------------------------------------------

    def _generate(
        self, ctx: PipelineContext, products: list[Product], *, system, min_queries, max_queries
    ) -> tuple[dict[int, list[enrichment.FacetSpec]], dict[int, str], str]:
        """One chat completion per product. A failure parks that product, not the batch."""
        generated: dict[int, list[enrichment.FacetSpec]] = {}
        failures: dict[int, str] = {}
        model = ""

        for product in products:
            try:
                response = ctx.llm.complete(
                    system=system,
                    user=enrichment.build_user_prompt(product),
                    json_schema=enrichment.RESPONSE_SCHEMA,
                    schema_name="product_facets",
                    purpose="enrich_products",
                )
            except LLMRequestFailed as exc:
                failures[product.pk] = str(exc)[:500]
                continue

            model = response.model
            specs = enrichment.parse_response(
                response.parsed or {}, min_queries=min_queries, max_queries=max_queries
            )
            if not specs:
                # Too few usable queries. Writing a half-enriched product would leave it ranking
                # badly forever, so park it and let a human look.
                failures[product.pk] = f"{product.asin}: consultas insuficientes"
                continue

            generated[product.pk] = specs

        return generated, failures, model

    @staticmethod
    def _embed(ctx: PipelineContext, generated: dict[int, list[enrichment.FacetSpec]]):
        """One embeddings call per batch — the facets have no reason to be embedded separately."""
        texts = [spec.text for specs in generated.values() for spec in specs]
        vectors: list[list[float]] = []
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            chunk = texts[start : start + EMBED_BATCH_SIZE]
            vectors.extend(ctx.llm.embed(chunk, purpose="enrich_products").vectors)
        return vectors

    @staticmethod
    def _write(generated, vectors, *, model: str) -> int:
        """Replace each product's facets atomically, then mark it DONE.

        Delete-then-create rather than update: the LLM does not return stable identities across
        runs, so there is nothing to match old rows against. Re-enriching is a clean replacement.
        """
        embedding_model = settings.OPENAI_EMBEDDING_MODEL
        cursor = 0
        rows: list[ProductFacet] = []
        product_ids = list(generated)

        for pid in product_ids:
            for spec in generated[pid]:
                rows.append(
                    ProductFacet(
                        product_id=pid,
                        text=spec.text,
                        facet_type=spec.facet_type,
                        weight=spec.weight,
                        occasions=spec.occasions,
                        recipients=spec.recipients,
                        interests=spec.interests,
                        embedding=vectors[cursor] if cursor < len(vectors) else None,
                        embedding_model=embedding_model,
                    )
                )
                cursor += 1

        with transaction.atomic():
            ProductFacet.objects.filter(product_id__in=product_ids).delete()
            ProductFacet.objects.bulk_create(rows, batch_size=200)
            Product.objects.filter(pk__in=product_ids).update(
                enrichment_status=EnrichmentStatus.DONE,
                enriched_at=timezone.now(),
                enrichment_model=model or settings.OPENAI_DEFAULT_MODEL,
            )
        return len(rows)

    @staticmethod
    def _mark_failed(failures: dict[int, str]) -> None:
        """FAILED products are retried on the next tick; the reason lives in the step context.

        Deliberately does not touch `availability_note` — that field belongs to the quality gate,
        and `recompute_derived` rewrites it. Conflating the two would make both meaningless.
        """
        if failures:
            Product.objects.filter(pk__in=list(failures)).update(
                enrichment_status=EnrichmentStatus.FAILED
            )
