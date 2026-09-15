from django.contrib import admin
from django.utils.html import format_html

from .models import Product, ProductFacet


class ProductFacetInline(admin.TabularInline):
    model = ProductFacet
    extra = 0
    fields = ("text", "facet_type", "occasions", "recipients", "interests", "weight", "has_vector")
    readonly_fields = ("has_vector",)
    show_change_link = True

    @admin.display(boolean=True, description="Vector")
    def has_vector(self, obj: ProductFacet) -> bool:
        return obj.embedding is not None


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "asin",
        "short_title",
        "brand",
        "price_band",
        "rating",
        "review_count",
        "quality_score",
        "enrichment_status",
        "is_active",
        "is_blocked",
        "keepa_fetched_at",
    )
    list_filter = (
        "enrichment_status",
        "is_active",
        "is_blocked",
        "is_adult",
        "price_band",
        "domain_id",
    )
    search_fields = ("asin", "title", "brand", "parent_asin", "variation_group_key")
    ordering = ("-quality_score",)
    list_per_page = 50
    date_hierarchy = "first_seen_at"
    inlines = [ProductFacetInline]
    readonly_fields = (
        "first_seen_at",
        "updated_at",
        "keepa_payload_hash",
        "quality_score",
        "ctr_score",
        "sales_rank_pct",
        "variation_group_key",
        "image_preview",
    )
    fieldsets = (
        (None, {"fields": ("asin", "domain_id", "title", "brand", "manufacturer", "model")}),
        (
            "Clasificación",
            {
                "fields": (
                    "parent_asin",
                    "variation_group_key",
                    "product_group",
                    "root_category_id",
                    "category_ids",
                )
            },
        ),
        (
            "Contenido",
            {"fields": ("features", "description_text", "image_urls", "image_preview")},
        ),
        (
            "Señales",
            {
                "fields": (
                    "rating",
                    "review_count",
                    "sales_rank",
                    "sales_rank_pct",
                    "quality_score",
                    "ctr_score",
                    "price_band",
                    "price_band_at",
                )
            },
        ),
        (
            "Ciclo de vida",
            {
                "fields": (
                    "is_active",
                    "availability_note",
                    "is_blocked",
                    "block_reason",
                    "is_adult",
                    "listed_since",
                    "keepa_fetched_at",
                    "keepa_payload_hash",
                    "first_seen_at",
                    "updated_at",
                )
            },
        ),
        (
            "Enriquecimiento",
            {"fields": ("enrichment_status", "enriched_at", "enrichment_model")},
        ),
    )
    actions = ("block_products", "unblock_products", "queue_for_reenrichment")

    @admin.display(description="Título", ordering="title")
    def short_title(self, obj: Product) -> str:
        return obj.title[:70]

    @admin.display(description="Imagen")
    def image_preview(self, obj: Product):
        if not obj.primary_image_url:
            return "—"
        return format_html(
            '<img src="{}" style="max-height:160px" loading="lazy">', obj.primary_image_url
        )

    @admin.action(description="Bloquear (no se mostrarán en el sitio)")
    def block_products(self, request, queryset):
        updated = queryset.update(is_blocked=True, block_reason="Bloqueado manualmente en Admin")
        self.message_user(request, f"{updated} productos bloqueados.")

    @admin.action(description="Desbloquear")
    def unblock_products(self, request, queryset):
        updated = queryset.update(is_blocked=False, block_reason="")
        self.message_user(request, f"{updated} productos desbloqueados.")

    @admin.action(description="Marcar para volver a enriquecer")
    def queue_for_reenrichment(self, request, queryset):
        updated = queryset.update(enrichment_status="PENDING")
        self.message_user(request, f"{updated} productos marcados como PENDING.")


@admin.register(ProductFacet)
class ProductFacetAdmin(admin.ModelAdmin):
    list_display = ("short_text", "product", "facet_type", "weight", "has_vector", "created_at")
    list_filter = ("facet_type", "embedding_model", "embedding_version")
    search_fields = ("text", "product__asin", "product__title")
    autocomplete_fields = ("product",)
    readonly_fields = ("created_at",)
    exclude = ("embedding",)
    list_per_page = 50

    @admin.display(description="Texto", ordering="text")
    def short_text(self, obj: ProductFacet) -> str:
        return obj.text[:90]

    @admin.display(boolean=True, description="Vector")
    def has_vector(self, obj: ProductFacet) -> bool:
        return obj.embedding is not None
