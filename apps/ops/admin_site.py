"""A custom ``AdminSite`` that adds a control panel alongside the ordinary admin.

``/admin/`` stays the stock Django index — the app and model list, exactly as it was — with one
banner linking to ``/admin/panel/``. The panel is a set of read-only monitoring pages: it answers
"what is happening and is it healthy", and hands you off to the normal changelist whenever you
want to change something. Nothing here writes to the database.

Why a custom site rather than a separate app: staff authentication, permissions, CSRF, messages,
breadcrumbs and the model navigation all already exist here.
"""

from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path
from django.utils import timezone

from apps.catalog.models import Product
from apps.ops import metrics
from apps.pipelines.models import KeepaTokenLedger, LLMCall, PipelineRun

#: (url name, label) for the panel's own navigation, in the order they appear.
PANEL_NAV = [
    ("ops_panel", "Resumen"),
    ("ops_pipelines", "Pipelines y trabajos"),
    ("ops_products", "Productos"),
    ("ops_topics", "Temas y términos"),
    ("ops_traffic", "Búsqueda y tráfico"),
    ("ops_cost", "Coste y presupuesto"),
]


class OpsAdminSite(admin.AdminSite):
    site_header = "Regalos Mejores"
    site_title = "Regalos Mejores"
    index_title = "Administración"
    #: The stock index plus a banner pointing at the panel.
    index_template = "admin/ops/admin_index.html"

    def get_urls(self):
        urls = [
            path("panel/", self.admin_view(self.panel), name="ops_panel"),
            path("panel/pipelines/", self.admin_view(self.panel_pipelines), name="ops_pipelines"),
            path("panel/productos/", self.admin_view(self.panel_products), name="ops_products"),
            path("panel/producto/<int:pk>/", self.admin_view(self.product), name="ops_product"),
            path("panel/temas/", self.admin_view(self.panel_topics), name="ops_topics"),
            path("panel/busqueda/", self.admin_view(self.panel_traffic), name="ops_traffic"),
            path("panel/coste/", self.admin_view(self.panel_cost), name="ops_cost"),
            path("panel/feed/", self.admin_view(self.feed_fragment), name="ops_feed"),
            path("panel/run/<int:run_id>/", self.admin_view(self.run_detail), name="ops_run"),
        ]
        # Ours first: none of these can collide with the generated model routes.
        return urls + super().get_urls()

    def index(self, request: HttpRequest, extra_context=None) -> HttpResponse:
        """The stock index, plus the database gauge — the one number worth seeing unprompted."""
        return super().index(request, {**(extra_context or {}), "db": metrics.database_capacity()})

    # --- plumbing ------------------------------------------------------------

    def _page(self, request: HttpRequest, template: str, title: str, **context) -> HttpResponse:
        return TemplateResponse(
            request,
            template,
            {
                **self.each_context(request),
                "title": title,
                "panel_nav": PANEL_NAV,
                "now": timezone.now(),
                **context,
            },
        )

    # --- pages ---------------------------------------------------------------

    def panel(self, request: HttpRequest) -> HttpResponse:
        """The hub: is anything on fire, what just happened, and where to go next."""
        return self._page(
            request,
            "admin/ops/panel_overview.html",
            "Panel de control",
            db=metrics.database_capacity(),
            worker=metrics.worker_health(),
            run_stats=metrics.run_totals(),
            catalog=metrics.catalog_health(),
            topics=metrics.topic_health(),
            traffic=metrics.traffic_panel(),
            cost=metrics.cost_panel(),
            events=metrics.event_feed(),
        )

    def panel_pipelines(self, request: HttpRequest) -> HttpResponse:
        return self._page(
            request,
            "admin/ops/panel_pipelines.html",
            "Pipelines y trabajos",
            worker=metrics.worker_health(),
            run_stats=metrics.run_totals(),
            pipelines=metrics.pipeline_grid(),
            queue=metrics.job_queue_panel(),
            runs=metrics.recent_runs(),
        )

    def panel_products(self, request: HttpRequest) -> HttpResponse:
        return self._page(
            request,
            "admin/ops/panel_products.html",
            "Productos",
            catalog=metrics.catalog_health(),
            funnel=metrics.product_funnel(),
            recent=metrics.recent_products(),
            stuck=metrics.stuck_products(),
        )

    def product(self, request: HttpRequest, pk: int) -> HttpResponse:
        product = get_object_or_404(Product, pk=pk)
        return self._page(
            request,
            "admin/ops/product_detail.html",
            f"{product.asin} · {product.title[:60]}",
            product=product,
            **metrics.product_detail(product),
        )

    def panel_topics(self, request: HttpRequest) -> HttpResponse:
        return self._page(
            request,
            "admin/ops/panel_topics.html",
            "Temas y términos",
            topics=metrics.topic_health(),
            board=metrics.topic_board(),
            terms=metrics.search_term_queue(),
        )

    def panel_traffic(self, request: HttpRequest) -> HttpResponse:
        return self._page(
            request,
            "admin/ops/panel_traffic.html",
            "Búsqueda y tráfico",
            traffic=metrics.traffic_panel(),
        )

    def panel_cost(self, request: HttpRequest) -> HttpResponse:
        return self._page(
            request,
            "admin/ops/panel_cost.html",
            "Coste y presupuesto",
            cost=metrics.cost_panel(),
            recent_calls=LLMCall.objects.select_related("pipeline_run").order_by("-at")[:25],
            recent_tokens=KeepaTokenLedger.objects.order_by("-at")[:15],
        )

    def feed_fragment(self, request: HttpRequest) -> HttpResponse:
        """The live event list on its own, for the HTMX poll."""
        return TemplateResponse(
            request,
            "admin/ops/_feed.html",
            {"events": metrics.event_feed(), "now": timezone.now()},
        )

    def run_detail(self, request: HttpRequest, run_id: int) -> HttpResponse:
        run = get_object_or_404(PipelineRun.objects.select_related("job"), pk=run_id)
        return self._page(
            request,
            "admin/ops/run_detail.html",
            f"Ejecución #{run.pk} · {run.pipeline_key}",
            run=run,
            steps=run.steps.order_by("started_at"),
            llm_calls=LLMCall.objects.filter(pipeline_run=run).order_by("at"),
            keepa_calls=KeepaTokenLedger.objects.filter(pipeline_run=run).order_by("at"),
            tone=metrics.run_tone(run),
            siblings=PipelineRun.objects.filter(pipeline_key=run.pipeline_key)
            .exclude(pk=run.pk)
            .order_by("-started_at")[:10],
        )
