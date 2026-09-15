"""Run one pipeline synchronously, bypassing the queue.

    uv run python manage.py run_pipeline demo_noop
    uv run python manage.py run_pipeline demo_noop --max-items 5
    uv run python manage.py run_pipeline seed_products --payload '{"category": 123}'
    uv run python manage.py run_pipeline --list

Goes through the same executor as the worker, so it produces identical run/step/cost records.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from apps.pipelines.models import RunStatus
from apps.pipelines.registry import PipelineNotFound, all_pipelines
from apps.pipelines.worker import execute


class Command(BaseCommand):
    help = "Run a single pipeline synchronously (manual testing and one-off jobs)."

    def add_arguments(self, parser):
        parser.add_argument("key", nargs="?", help="Pipeline key, e.g. demo_noop.")
        parser.add_argument("--payload", default="", help="JSON object passed to the pipeline.")
        parser.add_argument("--max-items", type=int, default=None)
        parser.add_argument(
            "--ignore-budget",
            action="store_true",
            help="Skip BudgetGuard. Use only when you know what you are spending.",
        )
        parser.add_argument("--list", action="store_true", help="List registered pipelines.")

    def handle(self, *args, **options):
        if options["list"] or not options["key"]:
            self._list()
            return

        payload = self._payload(options)
        try:
            run = execute(options["key"], payload, ignore_budget=options["ignore_budget"])
        except PipelineNotFound as exc:
            raise CommandError(str(exc)) from exc

        self._report(run)

    def _payload(self, options) -> dict:
        payload: dict = {}
        if options["payload"]:
            try:
                payload = json.loads(options["payload"])
            except ValueError as exc:
                raise CommandError(f"--payload is not valid JSON: {exc}") from exc
            if not isinstance(payload, dict):
                raise CommandError("--payload must be a JSON object")
        if options["max_items"] is not None:
            payload["max_items"] = options["max_items"]
        return payload

    def _list(self) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING("Registered pipelines"))
        for key, cls in all_pipelines().items():
            needs = ", ".join(
                filter(
                    None, ["keepa" if cls.requires_keepa else "", "llm" if cls.requires_llm else ""]
                )
            )
            self.stdout.write(
                f"  {key:20s} prio={cls.priority:<3} cron={cls.default_cron or '-':<12} "
                f"needs={needs or '-':<10} {cls.description}"
            )

    def _report(self, run) -> None:
        style = {
            RunStatus.SUCCESS: self.style.SUCCESS,
            RunStatus.SKIPPED: self.style.WARNING,
            RunStatus.FAILED: self.style.ERROR,
        }.get(run.status, self.style.NOTICE)

        self.stdout.write(style(f"\n{run.pipeline_key}: {run.status}"))
        if run.skip_reason:
            self.stdout.write(f"  skip reason: {run.skip_reason} {run.context.get('detail', '')}")
        if run.error:
            self.stdout.write(self.style.ERROR(f"  error: {run.error.splitlines()[0]}"))
        self.stdout.write(
            f"  run #{run.pk} · {run.duration_ms} ms · in={run.items_in} "
            f"created={run.items_created} updated={run.items_updated} failed={run.items_failed}"
        )
        self.stdout.write(f"  keepa tokens={run.keepa_tokens_used} · llm cost=${run.llm_cost_usd}")
        for step in run.steps.all():
            self.stdout.write(f"    - {step.name:16s} {step.status:8s} {step.duration_ms} ms")
