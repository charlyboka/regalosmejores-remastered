"""Catalog models: one row per ASIN, plus the LLM-generated facets we actually search over."""

from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector
from django.db import models
from pgvector.django import HalfVectorField, HnswIndex

#: Every embedding in the project uses this dimensionality (text-embedding-3-small @ 512).
EMBEDDING_DIMENSIONS = 512


class PriceBand(models.TextChoices):
    """Coarse qualitative band. We never store or display an actual price."""

    ECONOMICO = "ECONOMICO", "Económico"
    MEDIO = "MEDIO", "Medio"
    PREMIUM = "PREMIUM", "Premium"


class EnrichmentStatus(models.TextChoices):
    PENDING = "PENDING", "Pendiente"
    RUNNING = "RUNNING", "En curso"
    DONE = "DONE", "Completado"
    FAILED = "FAILED", "Fallido"
    SKIPPED = "SKIPPED", "Omitido"


class FacetType(models.TextChoices):
    SYNTHETIC_QUERY = "SYNTHETIC_QUERY", "Consulta sintética"
    GIFT_ANGLE = "GIFT_ANGLE", "Ángulo de regalo"
    SUMMARY = "SUMMARY", "Resumen"


class Product(models.Model):
    """One ASIN in one Keepa domain (locale)."""

    asin = models.CharField(max_length=10, db_index=True)
    domain_id = models.SmallIntegerField(default=9, help_text="Keepa domain id. 9 = amazon.es")

    # --- straight from Keepa -----------------------------------------------------------
    title = models.TextField()
    brand = models.CharField(max_length=255, blank=True, default="", db_index=True)
    manufacturer = models.CharField(max_length=255, blank=True, default="")
    model = models.CharField(max_length=255, blank=True, default="")
    parent_asin = models.CharField(max_length=10, blank=True, default="", db_index=True)
    product_group = models.CharField(
        max_length=255, blank=True, default="", help_text="Keepa `type`, p. ej. PHYSICAL_MOVIE."
    )
    binding = models.CharField(
        max_length=64, blank=True, default="", help_text="Keepa `binding`, p. ej. blu_ray."
    )
    root_category_id = models.BigIntegerField(null=True, blank=True, db_index=True)
    category_ids = ArrayField(models.BigIntegerField(), default=list, blank=True)
    features = ArrayField(models.TextField(), default=list, blank=True)
    description_text = models.TextField(blank=True)
    image_urls = ArrayField(models.URLField(max_length=500), default=list, blank=True)
    rating = models.DecimalField(max_digits=2, decimal_places=1, null=True, blank=True)
    review_count = models.IntegerField(null=True, blank=True)
    sales_rank = models.IntegerField(null=True, blank=True)
    is_adult = models.BooleanField(default=False)
    listed_since = models.DateTimeField(null=True, blank=True)

    # --- derived -----------------------------------------------------------------------
    # Internal only. Never rendered in a template, never exposed in a feed, never cached for
    # display: Amazon's Associates terms only permit showing prices obtained through PA-API.
    # We keep it solely to compute `price_band`, to spread prices across a result grid, and to
    # let band thresholds be re-tuned later without re-fetching every product from Keepa.
    price_cents = models.IntegerField(
        null=True, blank=True, help_text="Uso interno. Nunca se muestra al usuario."
    )
    # NULL here means "not computed yet", which is distinct from any band. Hence null=True.
    price_band = models.CharField(  # noqa: DJ001
        max_length=16, choices=PriceBand, null=True, blank=True
    )
    price_band_at = models.DateTimeField(null=True, blank=True)
    sales_rank_pct = models.FloatField(
        null=True, blank=True, help_text="0 = best, 1 = worst within the root category."
    )
    quality_score = models.FloatField(default=0, db_index=True)
    ctr_score = models.FloatField(default=0)
    variation_group_key = models.CharField(
        max_length=16,
        blank=True,
        db_index=True,
        help_text="parent_asin when present, otherwise asin. Collapses variations in results.",
    )

    # --- lifecycle ---------------------------------------------------------------------
    keepa_fetched_at = models.DateTimeField(null=True, blank=True, db_index=True)
    keepa_payload_hash = models.CharField(max_length=64, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True, db_index=True)
    availability_note = models.CharField(max_length=255, blank=True)
    is_blocked = models.BooleanField(default=False, help_text="Manual kill switch.")
    block_reason = models.CharField(max_length=255, blank=True)

    enrichment_status = models.CharField(
        max_length=16, choices=EnrichmentStatus, default=EnrichmentStatus.PENDING, db_index=True
    )
    enriched_at = models.DateTimeField(null=True, blank=True)
    enrichment_model = models.CharField(max_length=64, blank=True)

    class Meta:
        verbose_name = "producto"
        verbose_name_plural = "productos"
        constraints = [
            models.UniqueConstraint(fields=["asin", "domain_id"], name="uniq_product_asin_domain"),
        ]
        indexes = [
            models.Index(
                fields=["enrichment_status", "keepa_fetched_at"], name="product_enrich_fetched_idx"
            ),
            models.Index(fields=["is_active", "is_blocked"], name="product_serveable_idx"),
            models.Index(fields=["-quality_score"], name="product_quality_desc_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.asin} · {self.title[:60]}"

    @property
    def primary_image_url(self) -> str | None:
        return self.image_urls[0] if self.image_urls else None

    @property
    def gift_summary(self) -> str:
        """The "por qué es buen regalo" line shown on a card.

        Returns empty unless something has bulk-loaded it first. That is deliberate: a card is
        always rendered in a loop, and a lazy lookup here would be an N+1 query on every page.
        """
        return getattr(self, "_gift_summary", "")

    @property
    def amazon_detail_path(self) -> str:
        return f"/dp/{self.asin}"

    @property
    def amazon_reviews_path(self) -> str:
        return f"/product-reviews/{self.asin}/"


class ProductFacet(models.Model):
    """A synthetic Spanish gift query that this product answers. The unit of retrieval.

    We embed queries and compare them against other queries; comparing a user query against a
    product *title* matches badly because they live in different semantic spaces.
    """

    product = models.ForeignKey(Product, related_name="facets", on_delete=models.CASCADE)
    text = models.TextField()
    facet_type = models.CharField(
        max_length=20, choices=FacetType, default=FacetType.SYNTHETIC_QUERY
    )

    embedding = HalfVectorField(dimensions=EMBEDDING_DIMENSIONS, null=True, blank=True)
    embedding_model = models.CharField(max_length=64, blank=True)
    embedding_version = models.SmallIntegerField(default=1)

    occasions = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    recipients = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    interests = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    weight = models.FloatField(default=1.0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "faceta de producto"
        verbose_name_plural = "facetas de producto"
        indexes = [
            HnswIndex(
                name="facet_embedding_hnsw",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["halfvec_cosine_ops"],
            ),
            GinIndex(SearchVector("text", config="spanish_unaccent"), name="facet_text_fts_idx"),
            GinIndex(fields=["text"], name="facet_text_trgm_idx", opclasses=["gin_trgm_ops"]),
            GinIndex(fields=["occasions"], name="facet_occasions_idx"),
            GinIndex(fields=["recipients"], name="facet_recipients_idx"),
            GinIndex(fields=["interests"], name="facet_interests_idx"),
        ]

    def __str__(self) -> str:
        return self.text[:80]
