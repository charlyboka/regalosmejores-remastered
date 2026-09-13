from django.contrib import admin
from django.utils.html import format_html

from .models import ProductTopicLink, SearchTerm, Topic, TopicAlias, TopicStatus


class TopicAliasInline(admin.TabularInline):
    model = TopicAlias
    extra = 0
    fields = ("text", "normalized", "source", "hits")
    readonly_fields = ("hits",)


class SearchTermInline(admin.TabularInline):
    model = SearchTerm
    extra = 0
    fields = ("text", "normalized", "status", "priority", "products_found", "last_run_at")
    readonly_fields = ("products_found", "last_run_at")


class ProductTopicLinkInline(admin.TabularInline):
    model = ProductTopicLink
    extra = 0
    fields = ("rank", "product", "score", "source", "is_pinned", "is_excluded")
    readonly_fields = ("score", "source")
    autocomplete_fields = ("product",)
    ordering = ("rank",)


@admin.register(Topic)
class TopicAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "slug",
        "kind",
        "status",
        "priority",
        "linked_count",
        "distinct_categories",
        "distinct_brands",
        "quality_score",
        "is_indexable",
        "human_reviewed",
        "last_curated_at",
    )
    list_filter = ("status", "kind", "source", "is_indexable", "human_reviewed", "price_band")
    search_fields = ("title", "slug", "canonical_text", "aliases__text")
    prepopulated_fields = {"slug": ("title",)}
    ordering = ("-priority", "slug")
    list_per_page = 50
    inlines = [TopicAliasInline, SearchTermInline, ProductTopicLinkInline]
    autocomplete_fields = ("merged_into",)
    exclude = ("embedding",)
    readonly_fields = (
        "linked_count",
        "distinct_categories",
        "distinct_brands",
        "quality_score",
        "is_indexable",
        "last_curated_at",
        "created_at",
        "updated_at",
        "public_link",
    )
    fieldsets = (
        (None, {"fields": ("title", "slug", "public_link", "kind", "status", "priority")}),
        ("SEO", {"fields": ("meta_title", "meta_description", "intro_html")}),
        (
            "Semántica",
            {
                "fields": (
                    "canonical_text",
                    "embedding_version",
                    "occasions",
                    "recipients",
                    "interests",
                    "price_band",
                )
            },
        ),
        (
            "Calidad",
            {
                "fields": (
                    "linked_count",
                    "distinct_categories",
                    "distinct_brands",
                    "quality_score",
                    "is_indexable",
                    "human_reviewed",
                )
            },
        ),
        (
            "Ciclo de vida",
            {
                "fields": (
                    "source",
                    "merged_into",
                    "seasonal_start",
                    "seasonal_end",
                    "last_curated_at",
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )
    actions = ("mark_human_reviewed", "activate_topics", "archive_topics")

    @admin.display(description="URL pública")
    def public_link(self, obj: Topic):
        if not obj.pk:
            return "—"
        url = obj.get_absolute_url()
        return format_html('<a href="{}" target="_blank">{}</a>', url, url)

    @admin.action(description="Marcar como revisado por humano")
    def mark_human_reviewed(self, request, queryset):
        updated = queryset.update(human_reviewed=True)
        self.message_user(request, f"{updated} temas marcados como revisados.")

    @admin.action(description="Activar (publicar)")
    def activate_topics(self, request, queryset):
        updated = queryset.update(status=TopicStatus.ACTIVE)
        self.message_user(request, f"{updated} temas activados.")

    @admin.action(description="Archivar")
    def archive_topics(self, request, queryset):
        updated = queryset.update(status=TopicStatus.ARCHIVED, is_indexable=False)
        self.message_user(request, f"{updated} temas archivados.")


@admin.register(TopicAlias)
class TopicAliasAdmin(admin.ModelAdmin):
    list_display = ("text", "topic", "source", "hits", "created_at")
    list_filter = ("source",)
    search_fields = ("text", "normalized", "topic__title")
    autocomplete_fields = ("topic",)
    exclude = ("embedding",)


@admin.register(SearchTerm)
class SearchTermAdmin(admin.ModelAdmin):
    list_display = (
        "text",
        "topic",
        "status",
        "priority",
        "run_count",
        "products_found",
        "keepa_tokens",
        "last_run_at",
    )
    list_filter = ("status",)
    search_fields = ("text", "normalized", "topic__title")
    autocomplete_fields = ("topic",)
    actions = ("requeue_terms",)

    @admin.action(description="Volver a poner en PENDING")
    def requeue_terms(self, request, queryset):
        updated = queryset.update(status="PENDING")
        self.message_user(request, f"{updated} términos reencolados.")


@admin.register(ProductTopicLink)
class ProductTopicLinkAdmin(admin.ModelAdmin):
    list_display = ("topic", "product", "rank", "score", "source", "is_pinned", "is_excluded")
    list_filter = ("source", "is_pinned", "is_excluded")
    search_fields = ("topic__title", "topic__slug", "product__asin", "product__title")
    autocomplete_fields = ("topic", "product")
    ordering = ("topic", "rank")
    actions = ("pin_links", "exclude_links", "clear_overrides")

    @admin.action(description="Fijar (sobrevive al recálculo)")
    def pin_links(self, request, queryset):
        updated = queryset.update(is_pinned=True, is_excluded=False)
        self.message_user(request, f"{updated} enlaces fijados.")

    @admin.action(description="Excluir (sobrevive al recálculo)")
    def exclude_links(self, request, queryset):
        updated = queryset.update(is_excluded=True, is_pinned=False)
        self.message_user(request, f"{updated} enlaces excluidos.")

    @admin.action(description="Quitar fijado/exclusión")
    def clear_overrides(self, request, queryset):
        updated = queryset.update(is_pinned=False, is_excluded=False)
        self.message_user(request, f"{updated} enlaces restaurados a automático.")
