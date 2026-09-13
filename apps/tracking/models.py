"""First-party, cookieless analytics. No GA4, no third-party scripts, no consent banner."""

from django.db import models


class Placement(models.TextChoices):
    SIMPLE_SEARCH = "SIMPLE_SEARCH", "Búsqueda simple"
    ADVANCED_SEARCH = "ADVANCED_SEARCH", "Búsqueda avanzada"
    TOPIC_PAGE = "TOPIC_PAGE", "Página de tema"
    HOME = "HOME", "Home"
    RELATED = "RELATED", "Relacionados"
    ARTICLE = "ARTICLE", "Artículo"


class ClickButton(models.TextChoices):
    SEE_PRICE = "SEE_PRICE", "Ver precio"
    SEE_REVIEWS = "SEE_REVIEWS", "Ver reseñas"
    DETAILS = "DETAILS", "Ver detalles"
    IMAGE = "IMAGE", "Imagen"
    TITLE = "TITLE", "Título"


class ClickEvent(models.Model):
    """One outbound Amazon click. PROTECTs the product so history is never silently lost."""

    product = models.ForeignKey(
        "catalog.Product", on_delete=models.PROTECT, related_name="click_events"
    )
    topic = models.ForeignKey(
        "topics.Topic", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    user_query = models.ForeignKey(
        "search.UserQuery", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    placement = models.CharField(max_length=20, choices=Placement, db_index=True)
    button = models.CharField(max_length=16, choices=ClickButton, db_index=True)
    position = models.SmallIntegerField(null=True, blank=True, help_text="Rank in the list.")
    page_path = models.CharField(max_length=255, blank=True)
    referrer_host = models.CharField(max_length=255, blank=True)
    session_key = models.CharField(max_length=32, blank=True, db_index=True)
    ascsubtag = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "clic"
        verbose_name_plural = "clics"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["product", "-created_at"], name="click_product_recent_idx"),
            models.Index(fields=["placement", "button"], name="click_placement_button_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.product_id} · {self.button}"


class PageView(models.Model):
    path = models.CharField(max_length=255, db_index=True)
    topic = models.ForeignKey(
        "topics.Topic", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    session_key = models.CharField(max_length=32, blank=True, db_index=True)
    referrer_host = models.CharField(max_length=255, blank=True)
    is_bot = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "visita"
        verbose_name_plural = "visitas"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.path
