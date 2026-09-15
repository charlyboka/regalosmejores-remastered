"""A custom ``AdminSite`` whose landing page is an operations console.

Why a custom site rather than a separate app: staff authentication, permissions, CSRF, messages
and the model navigation all already exist here. The panel is an extra view on top, so the
ordinary changelists remain exactly where they were — reachable from the sidebar and linked from
the panel itself whenever you need the raw rows.

Everything on the panel is read-only except the explicit POST actions at the bottom of this
module, each of which re-checks the caller's permission for the model it touches.
"""

from __future__ import annotations

import logging

from django.contrib import admin, messages
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone

from apps.ops import metrics
from apps.pipelines.models import (
    JobQueue,
    JobStatus,
    KeepaTokenLedger,
    LLMCall,
    PipelineRun,
    PipelineSchedule,
)

logger = logging.getLogger(__name__)


class OpsAdminSite(admin.AdminSite):
    site_header = "Regalos Mejores"
    site_title = "Regalos Mejores"
    index_title = "Panel de control"
    index_template = None  # index() is overridden outright

    def get_urls(self):
        urls = [
            path("panel/feed/", self.admin_view(self.feed_fragment), name="ops_feed"),
            path("panel/run/<int:run_id>/", self.admin_view(self.run_detail), name="ops_run"),
            path("panel/action/", self.admin_view(self.action), name="ops_action"),
        ]
        # Ours first: none of these can collide with the generated model routes.
        return urls + super().get_urls()

    # --- pages ---------------------------------------------------------------

    def index(self, request: HttpRequest, extra_context=None) -> HttpResponse:
        context = {
            **self.each_context(request),
            "title": "Panel de control",
            "worker": metrics.worker_health(),
            "run_stats": metrics.run_totals(),
            "pipelines": metrics.pipeline_grid(),
            "cost": metrics.cost_panel(),
            "catalog": metrics.catalog_health(),
            "topics": metrics.topic_health(),
            "traffic": metrics.traffic_panel(),
            "events": metrics.event_feed(),
            "now": timezone.now(),
            **(extra_context or {}),
        }
        return TemplateResponse(request, "admin/ops/dashboard.html", context)

    def feed_fragment(self, request: HttpRequest) -> HttpResponse:
        """The live event list on its own, for the HTMX poll."""
        return TemplateResponse(
            request,
            "admin/ops/_feed.html",
            {"events": metrics.event_feed(), "now": timezone.now()},
        )

    def run_detail(self, request: HttpRequest, run_id: int) -> HttpResponse:
        run = get_object_or_404(PipelineRun.objects.select_related("job"), pk=run_id)
        context = {
            **self.each_context(request),
            "title": f"Ejecución #{run.pk} · {run.pipeline_key}",
            "run": run,
            "steps": run.steps.order_by("started_at"),
            "llm_calls": LLMCall.objects.filter(pipeline_run=run).order_by("at"),
            "keepa_calls": KeepaTokenLedger.objects.filter(pipeline_run=run).order_by("at"),
            "tone": metrics.run_tone(run),
            "siblings": PipelineRun.objects.filter(pipeline_key=run.pipeline_key)
            .exclude(pk=run.pk)
            .order_by("-started_at")[:10],
        }
        return TemplateResponse(request, "admin/ops/run_detail.html", context)

    # --- actions -------------------------------------------------------------

    def action(self, request: HttpRequest) -> HttpResponse:
        """Single POST endpoint for every panel button.

        CSRF and the staff check come from ``admin_view``; each branch additionally requires the
        Django permission for the model it changes, so a read-only staff account can look at the
        panel without being able to drive it.
        """
        if request.method != "POST":
            return HttpResponseRedirect(reverse("admin:index"))

        name = request.POST.get("action", "")
        handler = _ACTIONS.get(name)
        if handler is None:
            messages.error(request, "Acción desconocida.")
            return self._back(request)

        permission, run = handler
        if not request.user.has_perm(permission):
            messages.error(request, "No tienes permiso para esta acción.")
            return self._back(request)

        try:
            messages.success(request, run(request))
        except Exception as exc:
            logger.exception("Panel action %s failed", name)
            messages.error(request, f"La acción ha fallado: {exc}")
        return self._back(request)

    @staticmethod
    def _back(request: HttpRequest) -> HttpResponseRedirect:
        """Return to the page the button was on, never to an arbitrary URL."""
        target = request.POST.get("next", "")
        if target.startswith("/admin/") and "//" not in target[1:]:
            return HttpResponseRedirect(target)
        return HttpResponseRedirect(reverse("admin:index"))


# --- action implementations ------------------------------------------------------------------


def _schedule(request: HttpRequest) -> PipelineSchedule:
    return get_object_or_404(PipelineSchedule, pk=request.POST.get("id") or 0)


def _run_now(request: HttpRequest) -> str:
    schedule = _schedule(request)
    # Same semantics as the changelist action: the next tick enqueues it, which keeps all
    # deduplication and budget checks in the scheduler where they belong.
    PipelineSchedule.objects.filter(pk=schedule.pk).update(next_run_at=timezone.now())
    return f"{schedule.pipeline_key} se ejecutará en el próximo tick."


def _toggle_schedule(request: HttpRequest) -> str:
    schedule = _schedule(request)
    PipelineSchedule.objects.filter(pk=schedule.pk).update(enabled=not schedule.enabled)
    state = "desactivado" if schedule.enabled else "activado"
    return f"{schedule.pipeline_key} {state}."


def _reset_failures(request: HttpRequest) -> str:
    schedule = _schedule(request)
    PipelineSchedule.objects.filter(pk=schedule.pk).update(consecutive_failures=0)
    return f"Contador de fallos de {schedule.pipeline_key} puesto a cero."


def _retry_job(request: HttpRequest) -> str:
    job = get_object_or_404(JobQueue, pk=request.POST.get("id") or 0)
    JobQueue.objects.filter(pk=job.pk).update(
        status=JobStatus.QUEUED,
        attempts=0,
        available_at=timezone.now(),
        locked_by="",
        locked_at=None,
        last_error="",
    )
    return f"Trabajo #{job.pk} ({job.pipeline_key}) reencolado."


def _cancel_job(request: HttpRequest) -> str:
    job = get_object_or_404(JobQueue, pk=request.POST.get("id") or 0)
    JobQueue.objects.filter(pk=job.pk).update(status=JobStatus.FAILED, last_error="Cancelado")
    return f"Trabajo #{job.pk} cancelado."


def _retry_failed_jobs(request: HttpRequest) -> str:
    updated = JobQueue.objects.filter(status=JobStatus.FAILED).update(
        status=JobStatus.QUEUED,
        attempts=0,
        available_at=timezone.now(),
        locked_by="",
        locked_at=None,
        last_error="",
    )
    return f"{updated} trabajos fallidos reencolados."


#: action name -> (required permission, callable returning the success message)
_ACTIONS = {
    "run_now": ("pipelines.change_pipelineschedule", _run_now),
    "toggle_schedule": ("pipelines.change_pipelineschedule", _toggle_schedule),
    "reset_failures": ("pipelines.change_pipelineschedule", _reset_failures),
    "retry_job": ("pipelines.change_jobqueue", _retry_job),
    "cancel_job": ("pipelines.change_jobqueue", _cancel_job),
    "retry_failed_jobs": ("pipelines.change_jobqueue", _retry_failed_jobs),
}
