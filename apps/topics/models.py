"""Topics are the SEO assets: one curated landing page per gift intent."""

from django.contrib.postgres.fields import ArrayField
from django.db import models
from pgvector.django import HalfVectorField, HnswIndex

from apps.catalog.models import EMBEDDING_DIMENSIONS, PriceBand


class TopicKind(models.TextChoices):
    OCCASION = "OCCASION", "Ocasión"
    RECIPIENT = "RECIPIENT", "Para quién"
    INTEREST = "INTEREST", "Afición"
    BUDGET = "BUDGET", "Presupuesto"
    HYBRID = "HYBRID", "Híbrido"


class TopicSource(models.TextChoices):
    MANUAL = "MANUAL", "Manual"
    LLM = "LLM", "LLM"
    MINED_QUERY = "MINED_QUERY", "Demanda de búsqueda"
    MINED_GSC = "MINED_GSC", "Search Console"


class TopicStatus(models.TextChoices):
    DRAFT = "DRAFT", "Borrador"
    ACTIVE = "ACTIVE", "Activo"
    ARCHIVED = "ARCHIVED", "Archivado"


class AliasSource(models.TextChoices):
    MANUAL = "MANUAL", "Manual"
    MINED_QUERY = "MINED_QUERY", "Demanda de búsqueda"
    LLM = "LLM", "LLM"


class SearchTermStatus(models.TextChoices):
    PENDING = "PENDING", "Pendiente"
    RUNNING = "RUNNING", "En curso"
    DONE = "DONE", "Completado"
    FAILED = "FAILED", "Fallido"
    EXHAUSTED = "EXHAUSTED", "Agotado"


class LinkSource(models.TextChoices):
    AUTO = "AUTO", "Automático"
    MANUAL = "MANUAL", "Manual"
    PINNED = "PINNED", "Fijado"


class Topic(models.Model):
    """A gift theme with its own public landing page, e.g. "regalos para el día del padre"."""

    slug = models.SlugField(max_length=120, unique=True)
    title = models.CharField(max_length=200, help_text="H1 of the landing page.")
    meta_title = models.CharField(max_length=200, blank=True)
    meta_description = models.CharField(max_length=320, blank=True)
    intro_html = models.TextField(blank=True, help_text="Short, human-reviewed.")
    canonical_text = models.TextField(help_text="Normalised query form used for embedding.")

    embedding = HalfVectorField(dimensions=EMBEDDING_DIMENSIONS, null=True, blank=True)
    embedding_version = models.SmallIntegerField(default=1)

    kind = models.CharField(max_length=16, choices=TopicKind, default=TopicKind.HYBRID)
    occasions = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    recipients = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    interests = ArrayField(models.CharField(max_length=64), default=list, blank=True)
    # NULL means "no budget constraint", which is distinct from any band.
    price_band = models.CharField(  # noqa: DJ001
        max_length=16, choices=PriceBand, null=True, blank=True
    )

    source = models.CharField(max_length=16, choices=TopicSource, default=TopicSource.MANUAL)
    status = models.CharField(
        max_length=16, choices=TopicStatus, default=TopicStatus.DRAFT, db_index=True
    )
    priority = models.SmallIntegerField(default=50, help_text="Higher runs sooner in pipelines.")

    # --- quality gate (blueprint §9.2) --------------------------------------------------
    linked_count = models.IntegerField(default=0)
    distinct_categories = models.IntegerField(default=0)
    distinct_brands = models.IntegerField(default=0)
    quality_score = models.FloatField(default=0)
    is_indexable = models.BooleanField(
        default=False, db_index=True, help_text="Computed. Controls robots meta and the sitemap."
    )
    human_reviewed = models.BooleanField(default=False)

    merged_into = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="merged_from"
    )
    seasonal_start = models.DateField(null=True, blank=True)
    seasonal_end = models.DateField(null=True, blank=True)
    last_curated_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "tema"
        verbose_name_plural = "temas"
        ordering = ["-priority", "slug"]
        indexes = [
            models.Index(fields=["status", "is_indexable"], name="topic_status_indexable_idx"),
            HnswIndex(
                name="topic_embedding_hnsw",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["halfvec_cosine_ops"],
            ),
        ]

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        return f"/regalos/{self.slug}/"


class TopicAlias(models.Model):
    """A near-duplicate phrasing that resolves to a Topic. The dedup mechanism."""

    topic = models.ForeignKey(Topic, related_name="aliases", on_delete=models.CASCADE)
    text = models.TextField()
    normalized = models.CharField(max_length=255, unique=True)
    embedding = HalfVectorField(dimensions=EMBEDDING_DIMENSIONS, null=True, blank=True)
    source = models.CharField(max_length=16, choices=AliasSource, default=AliasSource.MANUAL)
    hits = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "alias de tema"
        verbose_name_plural = "alias de tema"
        indexes = [
            HnswIndex(
                name="alias_embedding_hnsw",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["halfvec_cosine_ops"],
            ),
        ]

    def __str__(self) -> str:
        return self.text[:80]


class SearchTerm(models.Model):
    """An Amazon keyword derived from a Topic. Sent to Keepa, never shown to users."""

    topic = models.ForeignKey(Topic, related_name="search_terms", on_delete=models.CASCADE)
    text = models.CharField(max_length=255)
    normalized = models.CharField(max_length=255, db_index=True)
    status = models.CharField(
        max_length=16, choices=SearchTermStatus, default=SearchTermStatus.PENDING, db_index=True
    )
    priority = models.SmallIntegerField(default=50)
    run_count = models.IntegerField(default=0)
    products_found = models.IntegerField(default=0)
    keepa_tokens = models.IntegerField(default=0)
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "término de búsqueda"
        verbose_name_plural = "términos de búsqueda"
        constraints = [
            models.UniqueConstraint(
                fields=["topic", "normalized"], name="uniq_searchterm_topic_normalized"
            ),
        ]

    def __str__(self) -> str:
        return self.text


class ProductTopicLink(models.Model):
    """A precomputed, ranked Product↔Topic association. The content of a landing page."""

    topic = models.ForeignKey(Topic, related_name="links", on_delete=models.CASCADE)
    product = models.ForeignKey(
        "catalog.Product", related_name="topic_links", on_delete=models.CASCADE
    )
    score = models.FloatField(db_index=True)
    rank = models.SmallIntegerField(default=0)
    source = models.CharField(max_length=16, choices=LinkSource, default=LinkSource.AUTO)
    is_pinned = models.BooleanField(default=False, help_text="Survives recompute.")
    is_excluded = models.BooleanField(default=False, help_text="Survives recompute.")
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "producto en tema"
        verbose_name_plural = "productos en tema"
        ordering = ["rank"]
        constraints = [
            models.UniqueConstraint(
                fields=["topic", "product"], name="uniq_producttopiclink_topic_product"
            ),
        ]
        indexes = [
            models.Index(fields=["topic", "rank"], name="link_topic_rank_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.topic.slug} #{self.rank}"
