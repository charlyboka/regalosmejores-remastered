"""Diagnostic pipelines. They touch no external API and no business data.

These stay in the codebase permanently: they are how you prove the engine itself is healthy
without spending Keepa tokens or LLM budget.
"""

from __future__ import annotations

import time

from apps.pipelines.base import Pipeline, PipelineContext, PipelineResult
from apps.pipelines.registry import register


@register
class DemoNoopPipeline(Pipeline):
    """Does nothing, slowly, in two steps. Proves scheduling, claiming and run tracking work."""

    key = "demo_noop"
    description = "Pipeline de diagnóstico: no hace nada, solo registra una ejecución."
    default_cron = ""  # on demand only; enable a cron from Admin to watch it tick
    default_max_per_run = 3
    priority = 90

    def run(self, ctx: PipelineContext) -> PipelineResult:
        with ctx.step("prepare"):
            time.sleep(0.2)

        processed = 0
        with ctx.step("process", max_items=ctx.max_items) as step:
            for _ in range(ctx.max_items):
                time.sleep(0.05)
                processed += 1
            step.context["processed"] = processed

        return PipelineResult(
            items_in=ctx.max_items,
            items_updated=processed,
            context={"note": "demo pipeline, no side effects", "payload": ctx.payload},
        )


@register
class DemoFailPipeline(Pipeline):
    """Always raises. Used to verify retry backoff and the Telegram ERROR on exhaustion."""

    key = "demo_fail"
    description = "Pipeline de diagnóstico: falla siempre, para probar reintentos."
    priority = 95
    max_attempts = 2

    def run(self, ctx: PipelineContext) -> PipelineResult:
        with ctx.step("explode"):
            raise RuntimeError("demo_fail always fails, on purpose")


@register
class DemoBudgetPipeline(Pipeline):
    """Declares an absurd Keepa appetite so BudgetGuard always defers it."""

    key = "demo_budget"
    description = "Pipeline de diagnóstico: siempre se aplaza por falta de presupuesto."
    priority = 95
    requires_keepa = True
    estimated_keepa_tokens = 10_000_000

    def run(self, ctx: PipelineContext) -> PipelineResult:  # pragma: no cover - never reached
        raise AssertionError("demo_budget should never actually run")
