"""The Postgres job queue.

Claiming uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so additional workers can be added later
without changing a line here. A job is only ever held inside a short transaction, so a worker
that is killed mid-job leaves the row reclaimable by :func:`release_stale_jobs`.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.pipelines.models import JobQueue, JobStatus

logger = logging.getLogger(__name__)

# Retry backoff per attempt number, in seconds. Index 0 is unused (attempts start at 1).
RETRY_BACKOFF_SECONDS = (0, 60, 300, 900)
MAX_BACKOFF_SECONDS = 3600
STALE_LOCK_MINUTES = 30


def enqueue(
    pipeline_key: str,
    payload: dict | None = None,
    *,
    priority: int = 50,
    available_at=None,
    dedupe_key: str = "",
    max_attempts: int = 3,
) -> JobQueue | None:
    """Add a job. Returns ``None`` when ``dedupe_key`` already has a live job."""
    try:
        with transaction.atomic():
            return JobQueue.objects.create(
                pipeline_key=pipeline_key,
                payload=payload or {},
                priority=priority,
                status=JobStatus.QUEUED,
                available_at=available_at or timezone.now(),
                dedupe_key=dedupe_key,
                max_attempts=max_attempts,
            )
    except IntegrityError:
        # The partial unique index rejected it: an identical job is already queued or running.
        logger.debug("Job %s deduped on key %r", pipeline_key, dedupe_key)
        return None


def claim_next(worker_name: str) -> JobQueue | None:
    """Atomically take the highest-priority due job, or return None."""
    now = timezone.now()
    with transaction.atomic():
        job = (
            JobQueue.objects.select_for_update(skip_locked=True)
            .filter(
                status__in=[JobStatus.QUEUED, JobStatus.DEFERRED],
                available_at__lte=now,
            )
            .order_by("priority", "available_at")
            .first()
        )
        if job is None:
            return None

        job.status = JobStatus.RUNNING
        job.locked_by = worker_name
        job.locked_at = now
        job.attempts += 1
        job.save(update_fields=["status", "locked_by", "locked_at", "attempts", "updated_at"])
        return job


def complete(job: JobQueue) -> None:
    job.status = JobStatus.DONE
    job.locked_by = ""
    job.locked_at = None
    job.last_error = ""
    job.save(update_fields=["status", "locked_by", "locked_at", "last_error", "updated_at"])


def defer(job: JobQueue, *, reason: str, retry_after_seconds: int) -> None:
    """Put the job back without burning an attempt.

    Deferral means "conditions were not right" (no tokens, budget spent, kill switch), not
    "this job is broken" — so it must not count towards ``max_attempts``.
    """
    job.status = JobStatus.DEFERRED
    job.available_at = timezone.now() + timedelta(seconds=max(retry_after_seconds, 1))
    job.attempts = max(job.attempts - 1, 0)
    job.locked_by = ""
    job.locked_at = None
    job.last_error = f"deferred: {reason}"[:2000]
    job.save(
        update_fields=[
            "status",
            "available_at",
            "attempts",
            "locked_by",
            "locked_at",
            "last_error",
            "updated_at",
        ]
    )


def fail(job: JobQueue, *, error: str) -> bool:
    """Record a failure. Returns True when the job is exhausted (no more retries)."""
    job.last_error = error[:4000]
    job.locked_by = ""
    job.locked_at = None

    exhausted = job.attempts >= job.max_attempts
    if exhausted:
        job.status = JobStatus.FAILED
    else:
        job.status = JobStatus.QUEUED
        job.available_at = timezone.now() + timedelta(seconds=_backoff(job.attempts))

    job.save(
        update_fields=[
            "status",
            "available_at",
            "locked_by",
            "locked_at",
            "last_error",
            "updated_at",
        ]
    )
    return exhausted


def release_stale_jobs(*, older_than_minutes: int = STALE_LOCK_MINUTES) -> int:
    """Reclaim jobs whose worker died while holding them (dyno restart, OOM, Ctrl+C)."""
    cutoff = timezone.now() - timedelta(minutes=older_than_minutes)
    stale = JobQueue.objects.filter(status=JobStatus.RUNNING, locked_at__lt=cutoff)
    count = stale.update(
        status=JobStatus.QUEUED,
        locked_by="",
        locked_at=None,
        available_at=timezone.now(),
        last_error="reclaimed after worker went away",
    )
    if count:
        logger.warning("Reclaimed %s stale job(s)", count)
    return count


def queue_depth() -> int:
    return JobQueue.objects.filter(
        status__in=[JobStatus.QUEUED, JobStatus.DEFERRED, JobStatus.RUNNING]
    ).count()


def _backoff(attempt: int) -> int:
    if attempt < len(RETRY_BACKOFF_SECONDS):
        return RETRY_BACKOFF_SECONDS[attempt]
    return MAX_BACKOFF_SECONDS
