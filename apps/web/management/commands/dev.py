"""Start the whole local stack with one command: web server + pipeline worker.

    uv run python manage.py dev            # both, Ctrl+C stops both
    uv run python manage.py dev --no-worker
    uv run python manage.py dev --port 8001

Each child inherits this console, so their logs interleave here and Ctrl+C reaches all of them.
If a child dies, the others are shut down too — a half-running stack is worse than a stopped one.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

SHUTDOWN_GRACE_SECONDS = 10
POLL_SECONDS = 0.4


class Command(BaseCommand):
    help = "Run the Django dev server and the pipeline worker together (local development)."

    def add_arguments(self, parser):
        parser.add_argument("--port", default="8000")
        parser.add_argument("--no-web", action="store_true", help="Worker only.")
        parser.add_argument("--no-worker", action="store_true", help="Web server only.")
        parser.add_argument("--worker-sleep", default="2", help="Worker idle sleep in seconds.")

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("`dev` is for local development only (DEBUG must be true).")
        if options["no_web"] and options["no_worker"]:
            raise CommandError("Nothing to run: --no-web and --no-worker are both set.")

        manage = str(Path(settings.BASE_DIR) / "manage.py")
        specs: list[tuple[str, list[str]]] = []
        if not options["no_web"]:
            specs.append(("web", [sys.executable, manage, "runserver", options["port"]]))
        if not options["no_worker"]:
            specs.append(
                (
                    "worker",
                    [sys.executable, manage, "run_worker", "--sleep", options["worker_sleep"]],
                )
            )

        processes: list[tuple[str, subprocess.Popen]] = []
        try:
            for name, command in specs:
                self.stdout.write(self.style.SUCCESS(f"starting {name}: {' '.join(command[1:])}"))
                processes.append((name, subprocess.Popen(command)))

            if not options["no_web"]:
                self.stdout.write(
                    self.style.MIGRATE_HEADING(
                        f"\n  site   http://127.0.0.1:{options['port']}/\n"
                        f"  admin  http://127.0.0.1:{options['port']}/admin/\n"
                    )
                )
            self.stdout.write(self.style.WARNING("Press Ctrl+C to stop everything.\n"))

            self._supervise(processes)
        except KeyboardInterrupt:
            self.stdout.write("\nStopping...")
        finally:
            self._shutdown(processes)
            self.stdout.write(self.style.SUCCESS("All processes stopped."))

    def _supervise(self, processes) -> None:
        """Block until any child exits."""
        while True:
            for name, process in processes:
                if process.poll() is not None:
                    self.stdout.write(
                        self.style.ERROR(f"\n{name} exited with code {process.returncode}")
                    )
                    return
            time.sleep(POLL_SECONDS)

    def _shutdown(self, processes) -> None:
        for name, process in processes:
            if process.poll() is not None:
                continue
            self.stdout.write(f"  stopping {name} (pid {process.pid})")
            try:
                # SIGTERM lets the worker finish its current job; the dev server exits at once.
                process.send_signal(signal.SIGTERM)
            except (OSError, ValueError):
                pass

        deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
        for name, process in processes:
            remaining = max(deadline - time.monotonic(), 0)
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                self.stdout.write(self.style.WARNING(f"  {name} did not stop; killing it"))
                process.kill()
