"""Worker loop and single-run executor.

``execute()`` is the one place a pipeline is ever run: the worker uses it, and so does
``manage.py run_pipeline``. That means a manual run produces exactly the same ``PipelineRun``,
step, token and cost records as a scheduled one.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from apps.clients.exceptions import KeepaBudgetExhausted, LLMBudgetExhausted
from apps.pipelines import queue as job_queue
from apps.pipelines.base import PipelineContext, PipelineResult
from apps.pipelines.budget import ALLOW, BudgetGuard, Decision
from apps.pipelines.models import (
    KeepaTokenLedger,
    LLMCall,
    NotificationLevel,
    PipelineRun,
    PipelineSchedule,
    RunStatus,
    WorkerHeartbeat,
)
from apps.pipelines.registry import get_pipeline
from apps.pipelines.scheduler import sync_schedules, tick

logger = logging.getLogger(__name__)

IDLE_SLEEP_SECONDS = 2.0
STALE_SWEEP_EVERY_SECONDS = 300


def execute(
    pipeline_key: str,
    payload: dict | None = None,
    *,
    job=None,
    ignore_budget: bool = False,
) -> PipelineRun:
    """Run one pipeline inside a ``PipelineRun``. Never raises for business failures.

    Returns the run row. ``run.status`` is SUCCESS, SKIPPED (budget) or FAILED.
    """
    payload = dict(payload or {})
    pipeline = get_pipeline(pipeline_key)

    decision = ALLOW if ignore_budget else BudgetGuard().check(pipeline)
    if not decision.allowed:
        return _skipped_run(pipeline_key, job, decision)

    schedule = PipelineSchedule.objects.filter(pipeline_key=pipeline_key).first()
    options = dict(schedule.options) if schedule else dict(pipeline.default_options)
    max_items = int(
        payload.pop("max_items", None)
        or (schedule.max_per_run if schedule else 0)
        or pipeline.default_max_per_run
    )

    run = PipelineRun.objects.create(pipeline_key=pipeline_key, job=job, status=RunStatus.RUNNING)
    ctx = PipelineContext(run=run, payload=payload, options=options, max_items=max_items)
    started = time.monotonic()

    try:
        result = pipeline.run(ctx) or PipelineResult()
    except Exception as exc:
        run.status = RunStatus.FAILED
        run.error = f"{type(exc).__name__}: {exc}"[:8000]
        if isinstance(exc, KeepaBudgetExhausted | LLMBudgetExhausted):
            # Ran out mid-flight. Not a defect — surface it as a skip reason too.
            run.skip_reason = type(exc).__name__
        logger.exception("Pipeline %s failed", pipeline_key)
    else:
        run.status = RunStatus.SUCCESS
        run.items_in = result.items_in
        run.items_created = result.items_created
        run.items_updated = result.items_updated
        run.items_failed = result.items_failed
        run.context = result.context

    run.finished_at = timezone.now()
    run.duration_ms = int((time.monotonic() - started) * 1000)
    run.keepa_tokens_used = (
        KeepaTokenLedger.objects.filter(pipeline_run=run).aggregate(t=Sum("tokens_consumed"))["t"]
        or 0
    )
    run.llm_cost_usd = LLMCall.objects.filter(pipeline_run=run).aggregate(c=Sum("cost_usd"))[
        "c"
    ] or Decimal("0")
    run.save()
    return run


def _skipped_run(pipeline_key: str, job, decision: Decision) -> PipelineRun:
    logger.info("Pipeline %s skipped: %s (%s)", pipeline_key, decision.reason, decision.detail)
    return PipelineRun.objects.create(
        pipeline_key=pipeline_key,
        job=job,
        status=RunStatus.SKIPPED,
        skip_reason=decision.reason,
        finished_at=timezone.now(),
        duration_ms=0,
        context={"detail": decision.detail, "retry_after_seconds": decision.retry_after_seconds},
    )


class Worker:
    def __init__(self, *, name: str = "", sleep_seconds: float = IDLE_SLEEP_SECONDS) -> None:
        # Stable per dyno/machine rather than per process, so restarts reuse one heartbeat row
        # instead of leaving a trail of dead workers behind.
        self.name = (name or os.environ.get("DYNO") or socket.gethostname())[:64]
        self.sleep_seconds = sleep_seconds
        self.should_stop = False
        self.jobs_processed = 0
        self.started_at = timezone.now()
        self._last_sweep = 0.0

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, self._request_stop)

    def _request_stop(self, signum, frame) -> None:
        logger.info("Signal %s received; finishing the current job then stopping", signum)
        self.should_stop = True

    def run_forever(self, *, once: bool = False) -> None:
        sync_schedules()
        logger.info("Worker %s started", self.name)
        while not self.should_stop:
            worked = self.run_once()
            if once:
                break
            if not worked:
                time.sleep(self.sleep_seconds)
        self._heartbeat(current_job=None)
        logger.info("Worker %s stopped after %s job(s)", self.name, self.jobs_processed)

    def run_once(self) -> bool:
        """One iteration. Returns True when a job was executed."""
        self._sweep_stale_jobs()
        self._heartbeat(current_job=None)

        try:
            tick()
        except Exception:
            logger.exception("Scheduler tick failed")

        job = job_queue.claim_next(self.name)
        if job is None:
            return False

        self._heartbeat(current_job=job)
        logger.info("Claimed %s #%s (attempt %s)", job.pipeline_key, job.pk, job.attempts)

        try:
            run = execute(job.pipeline_key, job.payload, job=job)
        except Exception as exc:
            # Only infrastructure errors reach here; execute() absorbs pipeline failures.
            logger.exception("Job %s crashed outside the run wrapper", job.pk)
            self._handle_failure(job, f"{type(exc).__name__}: {exc}")
        else:
            self._finish(job, run)

        self.jobs_processed += 1
        return True

    # --- outcome handling ----------------------------------------------------

    def _finish(self, job, run: PipelineRun) -> None:
        if run.status == RunStatus.SKIPPED:
            retry_after = int(run.context.get("retry_after_seconds") or 300)
            job_queue.defer(job, reason=run.skip_reason, retry_after_seconds=retry_after)
            return

        if run.status == RunStatus.FAILED:
            self._handle_failure(job, run.error)
            return

        job_queue.complete(job)
        self._record_schedule_outcome(job.pipeline_key, succeeded=True)

    def _handle_failure(self, job, error: str) -> None:
        exhausted = job_queue.fail(job, error=error)
        self._record_schedule_outcome(job.pipeline_key, succeeded=False)
        if not exhausted:
            return

        from apps.clients.telegram import notify

        notify(
            f"pipeline_failed:{job.pipeline_key}",
            f"El pipeline <b>{job.pipeline_key}</b> ha fallado {job.attempts} veces "
            f"y se ha detenido.\n<code>{error[:500]}</code>",
            level=NotificationLevel.ERROR,
            throttle_minutes=60,
        )

    @staticmethod
    def _record_schedule_outcome(pipeline_key: str, *, succeeded: bool) -> None:
        schedule = PipelineSchedule.objects.filter(pipeline_key=pipeline_key).first()
        if schedule is None:
            return
        schedule.consecutive_failures = 0 if succeeded else schedule.consecutive_failures + 1
        schedule.save(update_fields=["consecutive_failures"])

    # --- housekeeping --------------------------------------------------------

    def _sweep_stale_jobs(self) -> None:
        now = time.monotonic()
        if now - self._last_sweep < STALE_SWEEP_EVERY_SECONDS:
            return
        self._last_sweep = now
        try:
            job_queue.release_stale_jobs()
        except Exception:
            logger.exception("Stale job sweep failed")

    def _heartbeat(self, *, current_job) -> None:
        try:
            WorkerHeartbeat.objects.update_or_create(
                name=self.name,
                defaults={
                    "at": timezone.now(),
                    "pid": os.getpid(),
                    "current_job": current_job,
                    "jobs_processed": self.jobs_processed,
                    "started_at": self.started_at,
                },
            )
        except Exception:
            logger.exception("Heartbeat write failed")
