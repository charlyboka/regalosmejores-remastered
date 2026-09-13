"""The worker process. This is what the Heroku ``worker`` dyno runs.

uv run python manage.py run_worker
uv run python manage.py run_worker --once      # single iteration, for debugging
"""

from __future__ import annotations

import logging

from django.core.management.base import BaseCommand

from apps.pipelines.worker import Worker

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the background pipeline worker (scheduler + job queue consumer)."

    def add_arguments(self, parser):
        parser.add_argument("--name", default="", help="Worker name; defaults to host:pid.")
        parser.add_argument(
            "--sleep", type=float, default=2.0, help="Seconds to idle when the queue is empty."
        )
        parser.add_argument("--once", action="store_true", help="Do a single iteration and exit.")

    def handle(self, *args, **options):
        worker = Worker(name=options["name"], sleep_seconds=options["sleep"])
        worker.install_signal_handlers()
        self.stdout.write(self.style.SUCCESS(f"Worker {worker.name} starting. Ctrl+C to stop."))
        try:
            worker.run_forever(once=options["once"])
        except KeyboardInterrupt:
            self.stdout.write("\nInterrupted.")
        self.stdout.write(f"Processed {worker.jobs_processed} job(s).")
