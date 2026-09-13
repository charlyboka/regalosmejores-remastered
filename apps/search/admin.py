from django.contrib import admin

from .models import QueryDemand, RankingConfig, SearchResultCache, UserQuery


@admin.register(UserQuery)
class UserQueryAdmin(admin.ModelAdmin):
    list_display = (
        "raw_text",
        "mode",
        "matched_topic",
        "match_similarity",
        "result_count",
        "is_zero_result",
        "served_from_cache",
        "latency_ms",
        "created_at",
    )
    list_filter = ("mode", "is_zero_result", "served_from_cache")
    search_fields = ("raw_text", "normalized", "query_hash")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 100
    exclude = ("embedding",)
    readonly_fields = tuple(
        f.name for f in UserQuery._meta.fields if f.name not in {"id", "embedding"}
    )

    def has_add_permission(self, request) -> bool:
        return False


@admin.register(QueryDemand)
class QueryDemandAdmin(admin.ModelAdmin):
    list_display = (
        "day",
        "normalized",
        "hits",
        "zero_results",
        "clicks",
        "avg_similarity",
        "matched_topic",
        "promoted_topic",
    )
    list_filter = ("day",)
    search_fields = ("normalized",)
    date_hierarchy = "day"
    ordering = ("-day", "-hits")
    autocomplete_fields = ("matched_topic", "promoted_topic")
    list_per_page = 100


@admin.register(SearchResultCache)
class SearchResultCacheAdmin(admin.ModelAdmin):
    list_display = ("query_hash", "hits", "created_at", "expires_at")
    search_fields = ("query_hash",)
    ordering = ("-created_at",)
    actions = ("purge_selected",)

    @admin.action(description="Vaciar entradas seleccionadas")
    def purge_selected(self, request, queryset):
        deleted, _ = queryset.delete()
        self.message_user(request, f"{deleted} entradas de caché eliminadas.")


@admin.register(RankingConfig)
class RankingConfigAdmin(admin.ModelAdmin):
    """Singleton. Tuning ranking must never require a deploy."""

    list_display = (
        "__str__",
        "w_semantic",
        "w_lexical",
        "w_quality",
        "w_ctr",
        "w_freshness",
        "updated_at",
    )
    readonly_fields = ("updated_at",)
    fieldsets = (
        (
            "Pesos de ranking",
            {"fields": ("w_semantic", "w_lexical", "w_quality", "w_ctr", "w_freshness")},
        ),
        (
            "Umbrales",
            {"fields": ("topic_match_threshold", "min_facet_similarity", "candidate_pool_size")},
        ),
        (
            "Presentación y diversidad",
            {"fields": ("results_per_page", "max_per_brand", "max_per_category")},
        ),
        ("Caché", {"fields": ("cache_ttl_hours", "updated_at")}),
    )

    def has_add_permission(self, request) -> bool:
        return not RankingConfig.objects.exists()

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
