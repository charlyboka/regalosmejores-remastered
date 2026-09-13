"""Cron scheduler.

The worker ticks this on every loop. Enqueueing is idempotent via a ``dedupe_key`` built from
the scheduled slot, so a double tick — or two workers — cannot produce duplicate jobs.
"""

from __future__ import annotations

import logging

from croniter import CroniterBadCronError, croniter
from django.utils import timezone

from apps.pipelines.models import PipelineSchedule
from apps.pipelines.queue import enqueue
from apps.pipelines.registry import all_pipelines, get_pipeline

logger = logging.getLogger(__name__)


def sync_schedules() -> int:
    """Create a schedule row for every registered pipeline that declares a ``default_cron``.

    Existing rows are never overwritten — cron, options and ``max_per_run`` are owner-editable
    from Admin and must survive a deploy.
    """
    created = 0
    for key, cls in all_pipelines().items():
        if not cls.default_cron:
            continue
        _, was_created = PipelineSchedule.objects.get_or_create(
            pipeline_key=key,
            defaults={
                "enabled": True,
                "cron": cls.default_cron,
                "max_per_run": cls.default_max_per_run,
                "options": dict(cls.default_options),
                "next_run_at": next_run_after(cls.default_cron, timezone.now()),
            },
        )
        created += int(was_created)
    if created:
        logger.info("Created %s pipeline schedule(s)", created)
    return created


def tick(*, now=None) -> int:
    """Enqueue every schedule whose time has come. Returns how many jobs were created."""
    now = now or timezone.now()
    enqueued = 0

    for schedule in PipelineSchedule.objects.filter(enabled=True):
        if schedule.next_run_at is None:
            schedule.next_run_at = next_run_after(schedule.cron, now)
            schedule.save(update_fields=["next_run_at"])
            continue

        if schedule.next_run_at > now:
            continue

        slot = schedule.next_run_at
        try:
            pipeline = get_pipeline(schedule.pipeline_key)
        except Exception:
            logger.exception("Schedule %s refers to an unknown pipeline", schedule.pipeline_key)
            schedule.enabled = False
            schedule.save(update_fields=["enabled"])
            continue

        job = enqueue(
            schedule.pipeline_key,
            {"max_items": schedule.max_per_run, **dict(schedule.options)},
            priority=pipeline.priority,
            dedupe_key=f"sched:{schedule.pipeline_key}:{slot.isoformat()}",
            max_attempts=pipeline.max_attempts,
        )
        enqueued += int(job is not None)

        schedule.last_run_at = now
        schedule.next_run_at = next_run_after(schedule.cron, now)
        schedule.save(update_fields=["last_run_at", "next_run_at"])

    return enqueued


def next_run_after(cron: str, after) -> object | None:
    try:
        return croniter(cron, after).get_next(type(after))
    except (CroniterBadCronError, ValueError):
        logger.error("Invalid cron expression %r", cron)
        return None
