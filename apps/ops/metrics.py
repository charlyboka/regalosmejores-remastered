"""Every figure the control panel shows, gathered in one place.

Kept apart from the admin site so the panel stays presentation-only, and so any of these can be
reused later by a digest pipeline or a health endpoint without dragging the admin in.

Each function is deliberately a small number of aggregate queries. The panel runs roughly
twenty of them per load, which is fine for a single operator but is the reason nothing here
loops over rows in Python.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone

from apps.catalog.models import EnrichmentStatus, Product, ProductFacet
from apps.pipelines.models import (
    JobQueue,
    JobStatus,
    KeepaTokenLedger,
    LLMCall,
    NotificationLog,
    PipelineRun,
    PipelineSchedule,
    RunStatus,
    WorkerHeartbeat,
)
from apps.search.models import UserQuery
from apps.topics.models import Topic, TopicStatus
from apps.tracking.models import ClickEvent, PageView

#: A run slower than this is worth a second look, not an alarm.
SLOW_RUN_MS = 120_000


@dataclass(frozen=True, slots=True)
class Stat:
    """One headline number. ``tone`` drives the colour only."""

    label: str
    value: str
    detail: str = ""
    tone: str = "neutral"  # neutral | good | warn | bad


# --- workers and queue ---------------------------------------------------------------------


def worker_health() -> dict:
    workers = list(WorkerHeartbeat.objects.select_related("current_job").order_by("name"))
    alive = [w for w in workers if not w.is_stale]
    counts = dict(
        JobQueue.objects.values_list("status").annotate(n=Count("id")).values_list("status", "n")
    )

    queued = counts.get(JobStatus.QUEUED, 0)
    running = counts.get(JobStatus.RUNNING, 0)
    deferred = counts.get(JobStatus.DEFERRED, 0)
    failed = counts.get(JobStatus.FAILED, 0)

    oldest = (
        JobQueue.objects.filter(status=JobStatus.QUEUED)
        .order_by("available_at")
        .values_list("available_at", flat=True)
        .first()
    )

    return {
        "workers": workers,
        "stats": [
            Stat(
                "Workers vivos",
                f"{len(alive)}/{len(workers) or 0}",
                "latido < 15 min" if workers else "ningún worker ha arrancado todavía",
                tone="good" if alive else "bad",
            ),
            Stat(
                "En cola",
                str(queued),
                _age_phrase(oldest),
                tone="warn" if queued > 200 else "neutral",
            ),
            Stat("Ejecutándose", str(running), tone="neutral"),
            Stat(
                "Aplazados",
                str(deferred),
                "esperando presupuesto o tokens" if deferred else "",
                tone="warn" if deferred else "neutral",
            ),
            Stat(
                "Fallidos",
                str(failed),
                "agotaron los reintentos" if failed else "",
                tone="bad" if failed else "good",
            ),
        ],
    }


# --- pipelines -----------------------------------------------------------------------------


def pipeline_grid() -> list[dict]:
    """One row per schedule, with its most recent run attached."""
    schedules = list(PipelineSchedule.objects.order_by("pipeline_key"))
    last_runs = {}
    for run in PipelineRun.objects.order_by("pipeline_key", "-started_at").distinct("pipeline_key"):
        last_runs[run.pipeline_key] = run

    pending = dict(
        JobQueue.objects.filter(status__in=[JobStatus.QUEUED, JobStatus.RUNNING])
        .values_list("pipeline_key")
        .annotate(n=Count("id"))
        .values_list("pipeline_key", "n")
    )

    rows = []
    for schedule in schedules:
        run = last_runs.get(schedule.pipeline_key)
        rows.append(
            {
                "schedule": schedule,
                "last_run": run,
                "pending": pending.get(schedule.pipeline_key, 0),
                "tone": _schedule_tone(schedule, run),
            }
        )
    return rows


def _schedule_tone(schedule: PipelineSchedule, run: PipelineRun | None) -> str:
    if not schedule.enabled:
        return "off"
    if schedule.consecutive_failures >= 3:
        return "bad"
    if schedule.consecutive_failures:
        return "warn"
    if run is None:
        return "neutral"
    if run.status == RunStatus.FAILED:
        return "bad"
    if run.status == RunStatus.SKIPPED:
        return "warn"
    return "good"


def run_totals(hours: int = 24) -> list[Stat]:
    since = timezone.now() - timedelta(hours=hours)
    runs = PipelineRun.objects.filter(started_at__gte=since)
    by_status = dict(runs.values_list("status").annotate(n=Count("id")).values_list("status", "n"))
    ok = by_status.get(RunStatus.SUCCESS, 0)
    failed = by_status.get(RunStatus.FAILED, 0)
    skipped = by_status.get(RunStatus.SKIPPED, 0)
    totals = runs.aggregate(
        created=Sum("items_created"), updated=Sum("items_updated"), slow=Count("id")
    )
    return [
        Stat("Ejecuciones OK", str(ok), f"últimas {hours} h", tone="good" if ok else "neutral"),
        Stat("Fallidas", str(failed), f"últimas {hours} h", tone="bad" if failed else "good"),
        Stat(
            "Omitidas",
            str(skipped),
            "presupuesto o kill switch",
            tone="warn" if skipped else "neutral",
        ),
        Stat(
            "Ítems creados",
            str(totals["created"] or 0),
            f"{totals['updated'] or 0} actualizados",
        ),
    ]


# --- cost ----------------------------------------------------------------------------------


def cost_panel() -> dict:
    now = timezone.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def spend(since) -> Decimal:
        return LLMCall.objects.filter(at__gte=since).aggregate(c=Sum("cost_usd"))["c"] or Decimal(
            "0"
        )

    spent_today = spend(today)
    budget = Decimal(str(settings.LLM_DAILY_BUDGET_USD or 0))
    used_pct = int(spent_today / budget * 100) if budget else 0

    ledger = KeepaTokenLedger.objects.order_by("-at").first()
    projected = _projected_keepa_tokens(ledger, now)

    calls_today = LLMCall.objects.filter(at__gte=today).aggregate(
        n=Count("id"), failed=Count("id", filter=Q(success=False)), tokens=Sum("total_tokens")
    )

    return {
        "stats": [
            Stat(
                "Gasto LLM hoy",
                f"${spent_today:.2f}",
                f"{used_pct}% de ${budget:.2f}" if budget else "sin límite configurado",
                tone="bad" if used_pct >= 100 else "warn" if used_pct >= 75 else "good",
            ),
            Stat("Gasto 7 días", f"${spend(now - timedelta(days=7)):.2f}"),
            Stat("Gasto 30 días", f"${spend(now - timedelta(days=30)):.2f}"),
            Stat(
                "Tokens Keepa",
                str(projected) if projected is not None else "—",
                _ledger_phrase(ledger),
                tone=_keepa_tone(projected),
            ),
        ],
        "calls_today": calls_today["n"] or 0,
        "calls_failed_today": calls_today["failed"] or 0,
        "tokens_today": calls_today["tokens"] or 0,
        "budget_used_pct": min(used_pct, 100),
        "by_purpose": list(
            LLMCall.objects.filter(at__gte=now - timedelta(days=7))
            .values("purpose")
            .annotate(n=Count("id"), cost=Sum("cost_usd"))
            .order_by("-cost")[:8]
        ),
    }


def _projected_keepa_tokens(ledger: KeepaTokenLedger | None, now) -> int | None:
    """Same projection the budget guard uses, so the panel never disagrees with it."""
    if ledger is None:
        return None
    elapsed_minutes = max((now - ledger.at).total_seconds() / 60.0, 0.0)
    return int(ledger.tokens_left + elapsed_minutes * max(ledger.refill_rate or 0, 0))


def _keepa_tone(projected: int | None) -> str:
    if projected is None:
        return "neutral"
    reserve = getattr(settings, "KEEPA_TOKEN_RESERVE", 0)
    if projected <= reserve:
        return "bad"
    return "warn" if projected < reserve * 3 else "good"


def _ledger_phrase(ledger: KeepaTokenLedger | None) -> str:
    if ledger is None:
        return "sin lecturas todavía"
    return f"proyectado desde {ledger.at:%d/%m %H:%M}"


# --- catalogue and topics ------------------------------------------------------------------


def catalog_health() -> dict:
    agg = Product.objects.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(is_active=True, is_blocked=False)),
        blocked=Count("id", filter=Q(is_blocked=True)),
        unhydrated=Count("id", filter=Q(keepa_fetched_at__isnull=True)),
    )
    by_status = dict(
        Product.objects.values_list("enrichment_status")
        .annotate(n=Count("id"))
        .values_list("enrichment_status", "n")
    )
    facets = ProductFacet.objects.aggregate(
        total=Count("id"), missing=Count("id", filter=Q(embedding__isnull=True))
    )
    backlog_limit = getattr(settings, "MAX_HYDRATION_BACKLOG", 0)

    return {
        "stats": [
            Stat("Productos activos", str(agg["active"]), f"{agg['total']} en total"),
            Stat(
                "Sin hidratar",
                str(agg["unhydrated"]),
                f"límite de backlog {backlog_limit}" if backlog_limit else "",
                tone="warn" if backlog_limit and agg["unhydrated"] > backlog_limit else "neutral",
            ),
            Stat(
                "Pendientes de enriquecer",
                str(by_status.get(EnrichmentStatus.PENDING, 0)),
                f"{by_status.get(EnrichmentStatus.FAILED, 0)} fallidos",
                tone="warn" if by_status.get(EnrichmentStatus.FAILED, 0) else "neutral",
            ),
            Stat(
                "Facetas sin vector",
                str(facets["missing"] or 0),
                f"{facets['total'] or 0} facetas",
                tone="warn" if facets["missing"] else "good",
            ),
            Stat("Bloqueados", str(agg["blocked"]), tone="neutral"),
        ],
        "by_status": [
            {"label": EnrichmentStatus(key).label, "count": value}
            for key, value in sorted(by_status.items())
            if key in EnrichmentStatus.values
        ],
    }


def topic_health() -> dict:
    agg = Topic.objects.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(status=TopicStatus.ACTIVE)),
        draft=Count("id", filter=Q(status=TopicStatus.DRAFT)),
        indexable=Count("id", filter=Q(is_indexable=True, merged_into__isnull=True)),
        reviewed=Count("id", filter=Q(human_reviewed=True)),
        merged=Count("id", filter=Q(merged_into__isnull=False)),
    )
    # The §9.2 gate, expressed as a query so the panel and the curator agree.
    passing = Topic.objects.filter(
        merged_into__isnull=True,
        linked_count__gte=12,
        distinct_categories__gte=4,
        distinct_brands__gte=3,
    ).count()

    return {
        "stats": [
            Stat("Temas activos", str(agg["active"]), f"{agg['draft']} en borrador"),
            Stat(
                "Indexables",
                str(agg["indexable"]),
                f"{passing} cumplen la puerta §9.2",
                tone="good" if agg["indexable"] else "warn",
            ),
            Stat("Revisados a mano", str(agg["reviewed"]), f"de {agg['total']}"),
            Stat("Fusionados", str(agg["merged"]), "redirigen con 301"),
        ],
        "needs_review": list(
            Topic.objects.filter(status=TopicStatus.DRAFT, merged_into__isnull=True)
            .order_by("-linked_count", "title")[:8]
            .values("id", "title", "slug", "linked_count", "distinct_categories", "distinct_brands")
        ),
    }


# --- search and traffic --------------------------------------------------------------------


def traffic_panel(days: int = 7) -> dict:
    since = timezone.now() - timedelta(days=days)

    queries = UserQuery.objects.filter(created_at__gte=since).aggregate(
        n=Count("id"),
        zero=Count("id", filter=Q(is_zero_result=True)),
        cached=Count("id", filter=Q(served_from_cache=True)),
        redirected=Count("id", filter=Q(matched_topic__isnull=False)),
        latency=Avg("latency_ms"),
    )
    total = queries["n"] or 0
    zero_pct = int((queries["zero"] or 0) / total * 100) if total else 0

    views = PageView.objects.filter(created_at__gte=since).aggregate(
        n=Count("id", filter=Q(is_bot=False)),
        bots=Count("id", filter=Q(is_bot=True)),
        visitors=Count("session_key", distinct=True, filter=Q(is_bot=False)),
    )
    clicks = ClickEvent.objects.filter(created_at__gte=since).count()
    human_views = views["n"] or 0

    return {
        "days": days,
        "stats": [
            Stat("Visitas", str(human_views), f"{views['visitors'] or 0} visitantes"),
            Stat(
                "Búsquedas",
                str(total),
                f"{queries['redirected'] or 0} redirigidas a un tema",
            ),
            Stat(
                "Sin resultados",
                f"{zero_pct}%",
                f"{queries['zero'] or 0} búsquedas",
                tone="bad" if zero_pct >= 25 else "warn" if zero_pct >= 10 else "good",
            ),
            Stat(
                "Clics salientes",
                str(clicks),
                f"CTR {clicks / human_views * 100:.1f}%" if human_views else "",
                tone="good" if clicks else "neutral",
            ),
            Stat(
                "Latencia media",
                f"{int(queries['latency'])} ms" if queries["latency"] else "—",
                f"{queries['cached'] or 0} desde caché",
                tone="warn" if (queries["latency"] or 0) > 1000 else "neutral",
            ),
            Stat("Tráfico de bots", str(views["bots"] or 0), "excluido de las cifras"),
        ],
        "recent_queries": list(
            UserQuery.objects.select_related("matched_topic").order_by("-created_at")[:12]
        ),
        "zero_result_queries": list(
            UserQuery.objects.filter(is_zero_result=True, created_at__gte=since)
            .values("normalized")
            .annotate(n=Count("id"))
            .order_by("-n")[:10]
        ),
        "top_queries": list(
            UserQuery.objects.filter(created_at__gte=since)
            .values("normalized")
            .annotate(n=Count("id"))
            .order_by("-n")[:10]
        ),
        "clicks_by_placement": list(
            ClickEvent.objects.filter(created_at__gte=since)
            .values("placement")
            .annotate(n=Count("id"))
            .order_by("-n")
        ),
        "clicks_by_button": list(
            ClickEvent.objects.filter(created_at__gte=since)
            .values("button")
            .annotate(n=Count("id"))
            .order_by("-n")
        ),
        "top_pages": list(
            PageView.objects.filter(created_at__gte=since, is_bot=False)
            .values("path")
            .annotate(n=Count("id"))
            .order_by("-n")[:10]
        ),
    }


# --- event feed ----------------------------------------------------------------------------


def event_feed(limit: int = 40) -> list[dict]:
    """Runs and notifications interleaved, newest first.

    Two small queries merged in Python rather than a UNION: the volumes are tiny and this keeps
    the shape of each row honest instead of flattening both into a lowest common denominator.
    """
    events: list[dict] = []

    runs = PipelineRun.objects.order_by("-started_at")[:limit]
    for run in runs:
        events.append(
            {
                "at": run.started_at,
                "kind": "run",
                "tone": run_tone(run),
                "title": run.pipeline_key,
                "detail": _run_detail(run),
                "run_id": run.pk,
            }
        )

    for note in NotificationLog.objects.order_by("-sent_at")[: limit // 2]:
        events.append(
            {
                "at": note.sent_at,
                "kind": "notification",
                "tone": _notification_tone(note.level),
                "title": note.key,
                "detail": _strip_tags(note.message)[:160]
                + (" · suprimida" if note.suppressed else ""),
                "run_id": None,
            }
        )

    events.sort(key=lambda e: e["at"], reverse=True)
    return events[:limit]


def run_tone(run: PipelineRun) -> str:
    if run.status == RunStatus.FAILED:
        return "bad"
    if run.status == RunStatus.SKIPPED:
        return "warn"
    if run.status == RunStatus.RUNNING:
        return "info"
    if run.duration_ms and run.duration_ms > SLOW_RUN_MS:
        return "warn"
    return "good"


def _run_detail(run: PipelineRun) -> str:
    if run.status == RunStatus.FAILED:
        return (run.error or "sin detalle")[:160]
    if run.status == RunStatus.SKIPPED:
        return run.skip_reason or "omitida"
    if run.status == RunStatus.RUNNING:
        return "en curso"
    parts = [f"{run.items_created} creados", f"{run.items_updated} actualizados"]
    if run.items_failed:
        parts.append(f"{run.items_failed} fallidos")
    if run.duration_ms:
        parts.append(f"{run.duration_ms / 1000:.1f} s")
    if run.llm_cost_usd:
        parts.append(f"${run.llm_cost_usd:.4f}")
    if run.keepa_tokens_used:
        parts.append(f"{run.keepa_tokens_used} tokens")
    return " · ".join(parts)


def _notification_tone(level: str) -> str:
    return {"INFO": "info", "WARNING": "warn", "ERROR": "bad", "CRITICAL": "bad"}.get(
        level, "neutral"
    )


def _strip_tags(text: str) -> str:
    from django.utils.html import strip_tags

    return strip_tags(text)


# --- helpers -------------------------------------------------------------------------------


def _age_phrase(moment) -> str:
    if moment is None:
        return "cola vacía"
    delta = timezone.now() - moment
    minutes = int(delta.total_seconds() / 60)
    if minutes < 1:
        return "recién encolado"
    if minutes < 60:
        return f"el más antiguo, hace {minutes} min"
    return f"el más antiguo, hace {minutes // 60} h"
