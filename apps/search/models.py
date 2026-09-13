"""Search-side models: what users asked for, what we served, and how we rank."""

from django.db import models
from pgvector.django import HalfVectorField

from apps.catalog.models import EMBEDDING_DIMENSIONS


class SearchMode(models.TextChoices):
    SIMPLE = "SIMPLE", "Simple"
    ADVANCED = "ADVANCED", "Avanzada"


class UserQuery(models.Model):
    """One real visitor search. No PII: session_key is a salted hash."""

    raw_text = models.TextField()
    normalized = models.CharField(max_length=255, db_index=True)
    query_hash = models.CharField(max_length=32, db_index=True, help_text="md5(normalized+slots)")
    embedding = HalfVectorField(dimensions=EMBEDDING_DIMENSIONS, null=True, blank=True)
    mode = models.CharField(
        max_length=16, choices=SearchMode, default=SearchMode.SIMPLE, db_index=True
    )
    slots = models.JSONField(default=dict, blank=True)

    matched_topic = models.ForeignKey(
        "topics.Topic", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    match_similarity = models.FloatField(null=True, blank=True)
    result_count = models.IntegerField(default=0)
    is_zero_result = models.BooleanField(default=False, db_index=True)
    served_from_cache = models.BooleanField(default=False)
    latency_ms = models.IntegerField(null=True, blank=True)

    session_key = models.CharField(max_length=32, blank=True, db_index=True)
    referrer_host = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "consulta de usuario"
        verbose_name_plural = "consultas de usuario"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at", "is_zero_result"], name="userquery_recent_zero_idx")
        ]

    def __str__(self) -> str:
        return self.raw_text[:80]


class QueryDemand(models.Model):
    """Nightly roll-up of normalised queries. Drives what we build next."""

    normalized = models.CharField(max_length=255, db_index=True)
    day = models.DateField(db_index=True)
    hits = models.IntegerField(default=0)
    zero_results = models.IntegerField(default=0)
    clicks = models.IntegerField(default=0)
    avg_similarity = models.FloatField(null=True, blank=True)
    matched_topic = models.ForeignKey(
        "topics.Topic", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    promoted_topic = models.ForeignKey(
        "topics.Topic",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
        help_text="Set when this demand was turned into a Topic.",
    )

    class Meta:
        verbose_name = "demanda de búsqueda"
        verbose_name_plural = "demanda de búsqueda"
        ordering = ["-day", "-hits"]
        constraints = [
            models.UniqueConstraint(
                fields=["normalized", "day"], name="uniq_querydemand_normalized_day"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.day} · {self.normalized} ({self.hits})"


class SearchResultCache(models.Model):
    query_hash = models.CharField(max_length=32, unique=True)
    payload = models.JSONField(help_text="Ordered product ids plus metadata.")
    hits = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        verbose_name = "caché de resultados"
        verbose_name_plural = "caché de resultados"

    def __str__(self) -> str:
        return self.query_hash


class RankingConfig(models.Model):
    """Singleton. Editable in Admin so ranking can be tuned without a deploy."""

    w_semantic = models.FloatField(default=0.45)
    w_lexical = models.FloatField(default=0.15)
    w_quality = models.FloatField(default=0.20)
    w_ctr = models.FloatField(default=0.10)
    w_freshness = models.FloatField(default=0.10)

    topic_match_threshold = models.FloatField(
        default=0.90, help_text="Cosine similarity above which a search redirects to a topic page."
    )
    min_facet_similarity = models.FloatField(default=0.55)
    candidate_pool_size = models.IntegerField(default=200)
    results_per_page = models.IntegerField(default=24)
    max_per_brand = models.SmallIntegerField(default=2)
    max_per_category = models.SmallIntegerField(default=3)
    cache_ttl_hours = models.IntegerField(default=24)

    # Budget bands. Absolute euros on purpose: a shopper's wallet is absolute, so "económico"
    # has to mean cheap, not "cheap for a camera". Category-relative spread is handled by the
    # diversity pass instead. Changing these only needs `recompute_derived` afterwards.
    price_band_economico_max_cents = models.IntegerField(
        default=2500, help_text="Hasta este importe, ECONOMICO. Por defecto 25 €."
    )
    price_band_medio_max_cents = models.IntegerField(
        default=7500, help_text="Hasta este importe, MEDIO. Por encima, PREMIUM. Por defecto 75 €."
    )

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "configuración de ranking"
        verbose_name_plural = "configuración de ranking"

    def __str__(self) -> str:
        return "Configuración de ranking"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "RankingConfig":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj
