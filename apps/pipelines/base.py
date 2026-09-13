"""Pipeline base classes.

A pipeline declares *what* it needs (Keepa tokens, LLM budget) and *how often* it should run.
The worker owns everything else: claiming, budget gating, run tracking, retries and reporting.
Pipelines therefore contain business logic only.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from django.utils import timezone

from apps.pipelines.models import PipelineRun, PipelineStepRun, RunStatus

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    """What a pipeline reports back. Copied onto the ``PipelineRun`` row by the worker."""

    items_in: int = 0
    items_created: int = 0
    items_updated: int = 0
    items_failed: int = 0
    context: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return (
            f"in={self.items_in} created={self.items_created} "
            f"updated={self.items_updated} failed={self.items_failed}"
        )


class PipelineContext:
    """Everything a pipeline needs at runtime, bound to one ``PipelineRun``.

    Clients are created lazily and bound to the run, so every Keepa token and every cent of LLM
    spend is attributed to the run that caused it.
    """

    def __init__(self, *, run: PipelineRun, payload: dict, options: dict, max_items: int) -> None:
        self.run = run
        self.payload = payload or {}
        self.options = options or {}
        self.max_items = max_items
        self.logger = logging.getLogger(f"pipelines.{run.pipeline_key}")
        self._keepa = None
        self._llm = None

    @property
    def keepa(self):
        if self._keepa is None:
            from apps.clients.keepa import KeepaClient

            self._keepa = KeepaClient(pipeline_run=self.run)
        return self._keepa

    @property
    def llm(self):
        if self._llm is None:
            from apps.clients.llm import LLMClient

            self._llm = LLMClient(pipeline_run=self.run)
        return self._llm

    def option(self, name: str, default=None):
        """Payload overrides schedule options, which override the pipeline default."""
        if name in self.payload:
            return self.payload[name]
        return self.options.get(name, default)

    @contextmanager
    def step(self, name: str, **context):
        """Record a named phase of the run, with timing, so Admin shows where time went."""
        step = PipelineStepRun.objects.create(run=self.run, name=name, context=context or {})
        started = time.monotonic()
        try:
            yield step
        except Exception as exc:
            step.status = RunStatus.FAILED
            step.error = f"{type(exc).__name__}: {exc}"[:4000]
            raise
        else:
            step.status = RunStatus.SUCCESS
        finally:
            step.finished_at = timezone.now()
            step.duration_ms = int((time.monotonic() - started) * 1000)
            step.save(update_fields=["status", "error", "finished_at", "duration_ms", "context"])


class Pipeline(ABC):
    """Base class for every background job.

    Subclasses set ``key`` and implement ``run()``. Declaring ``requires_keepa`` /
    ``requires_llm`` is what lets :class:`~apps.pipelines.budget.BudgetGuard` defer the job
    *before* any expensive work starts.
    """

    key: str = ""
    description: str = ""
    default_cron: str = ""  # empty means "on demand only", no schedule row is created
    default_max_per_run: int = 10
    default_options: dict[str, Any] = {}
    priority: int = 50  # lower runs sooner
    max_attempts: int = 3

    requires_keepa: bool = False
    requires_llm: bool = False
    estimated_keepa_tokens: int = 0

    @abstractmethod
    def run(self, ctx: PipelineContext) -> PipelineResult:
        """Do the work. Raise to fail the job; the worker handles retries and reporting."""

    def __str__(self) -> str:
        return self.key
