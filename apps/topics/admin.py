from django.contrib import admin
from django.utils.html import format_html

from apps.pipelines.queue import enqueue

from .models import ProductTopicLink, SearchTerm, Topic, TopicAlias, TopicStatus
from .services import TopicQualityGate

#: The same gate `curate_topics` applies, so the changelist and the curator can never disagree
#: about why a topic is not indexable.
GATE = TopicQualityGate()

LIFECYCLE_HELP = (
    "Un tema recién creado recorre este camino: <b>decompose_topic</b> (04:00) lo convierte en "
    "términos de búsqueda de Amazon, <b>run_search_terms</b> (cada 4 h) trae productos "
    "candidatos, la hidratación y el enriquecimiento los completan, y <b>curate_topics</b> "
    "(05:00) los enlaza y aplica la puerta de calidad. Para no esperar, usa la acción "
    "<b>Procesar ahora</b> desde el listado."
)


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
        "kind",
        "status",
        "priority",
        "linked_count",
        "distinct_categories",
        "distinct_brands",
        "is_indexable",
        "blocking",
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
        "blocking",
        "last_curated_at",
        "created_at",
        "updated_at",
        "public_link",
    )
    fieldsets = (
        (
            None,
            {
                "fields": ("title", "slug", "public_link", "kind", "status", "priority"),
                "description": LIFECYCLE_HELP,
            },
        ),
        (
            "Para quién y para qué",
            {
                "fields": ("recipients", "occasions", "interests", "price_band"),
                "description": (
                    "Estas facetas deciden qué productos se buscan y actúan como filtro duro al "
                    "enlazarlos. Para «regalos para aniversario de bodas»: ocasión "
                    "<code>aniversario</code>, destinatario <code>pareja</code>."
                ),
            },
        ),
        (
            "SEO",
            {
                "fields": ("meta_title", "meta_description", "intro_html"),
                "description": (
                    "La introducción es obligatoria para que el tema sea indexable. Los otros dos "
                    "son opcionales: sin ellos se usan el título y un resumen."
                ),
            },
        ),
        (
            "Semántica",
            {
                "classes": ("collapse",),
                "fields": ("canonical_text", "embedding_version"),
                "description": (
                    "Se calculan solos en la primera curación, a partir del título y las facetas. "
                    "Déjalos en blanco salvo que quieras forzar una redacción concreta."
                ),
            },
        ),
        (
            "Calidad",
            {
                "fields": (
                    "blocking",
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
    actions = ("process_now", "mark_human_reviewed", "activate_topics", "archive_topics")

    @admin.display(description="URL pública")
    def public_link(self, obj: Topic):
        if not obj.pk:
            return "—"
        url = obj.get_absolute_url()
        return format_html('<a href="{}" target="_blank">{}</a>', url, url)

    @admin.display(description="Qué le falta")
    def blocking(self, obj: Topic):
        if not obj.pk:
            return "—"
        reason = GATE.evaluate(obj)
        if not reason:
            return format_html('<b style="color:var(--object-tools-bg,#417690)">Cumple</b>')
        return format_html('<span style="color:#b35c00">{}</span>', reason)

    @admin.action(description="Procesar ahora (descomponer, buscar productos y curar)")
    def process_now(self, request, queryset):
        """Jump the queue for the selected topics instead of waiting for tomorrow's cron.

        The three jobs are enqueued at top priority in dependency order; the single worker runs
        them one at a time, so terms exist before Keepa is asked and products exist before they
        are linked.
        """
        topic_ids = list(queryset.values_list("id", flat=True))
        for priority, key in enumerate(("decompose_topic", "run_search_terms", "curate_topics"), 1):
            enqueue(key, {"topic_ids": topic_ids}, priority=priority)
        self.message_user(
            request,
            f"{len(topic_ids)} temas encolados. Los productos recién descubiertos aún deben "
            "hidratarse y enriquecerse antes de poder enlazarse, así que puede hacer falta una "
            "segunda curación.",
        )

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
