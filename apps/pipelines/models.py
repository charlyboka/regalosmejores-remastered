"""Queue, scheduler and observability tables for the background pipelines.

There is no Redis and no Celery: concurrency is 1, jobs are short, and we already need run
tracking for the Admin dashboard. A Postgres queue with FOR UPDATE SKIP LOCKED is free,
fully visible in Admin, and trivially swappable later.
"""

from django.db import models


class JobStatus(models.TextChoices):
    QUEUED = "QUEUED", "En cola"
    RUNNING = "RUNNING", "Ejecutando"
    DONE = "DONE", "Completado"
    FAILED = "FAILED", "Fallido"
    DEFERRED = "DEFERRED", "Aplazado"


class RunStatus(models.TextChoices):
    RUNNING = "RUNNING", "Ejecutando"
    SUCCESS = "SUCCESS", "Correcto"
    FAILED = "FAILED", "Fallido"
    SKIPPED = "SKIPPED", "Omitido"


class NotificationLevel(models.TextChoices):
    INFO = "INFO", "Info"
    WARNING = "WARNING", "Aviso"
    ERROR = "ERROR", "Error"
    CRITICAL = "CRITICAL", "Crítico"


class PipelineSchedule(models.Model):
    pipeline_key = models.CharField(max_length=64, unique=True)
    enabled = models.BooleanField(default=True)
    cron = models.CharField(max_length=64, help_text="5-field cron expression, UTC.")
    max_per_run = models.IntegerField(default=10)
    options = models.JSONField(default=dict, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.IntegerField(default=0)

    class Meta:
        verbose_name = "programación"
        verbose_name_plural = "programaciones"
        ordering = ["pipeline_key"]

    def __str__(self) -> str:
        return f"{self.pipeline_key} ({self.cron})"


class JobQueue(models.Model):
    pipeline_key = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    priority = models.SmallIntegerField(default=50, db_index=True, help_text="Lower runs sooner.")
    status = models.CharField(
        max_length=16, choices=JobStatus, default=JobStatus.QUEUED, db_index=True
    )
    available_at = models.DateTimeField(db_index=True)
    attempts = models.SmallIntegerField(default=0)
    max_attempts = models.SmallIntegerField(default=3)
    locked_by = models.CharField(max_length=64, blank=True)
    locked_at = models.DateTimeField(null=True, blank=True)
    dedupe_key = models.CharField(max_length=128, blank=True, db_index=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "trabajo en cola"
        verbose_name_plural = "trabajos en cola"
        ordering = ["priority", "available_at"]
        constraints = [
            # Only one live job per dedupe_key. Finished jobs do not block re-enqueueing.
            models.UniqueConstraint(
                fields=["dedupe_key"],
                condition=models.Q(
                    status__in=[JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.DEFERRED]
                )
                & ~models.Q(dedupe_key=""),
                name="uniq_jobqueue_active_dedupe_key",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "priority", "available_at"], name="job_claim_order_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.pipeline_key} #{self.pk} ({self.status})"


class PipelineRun(models.Model):
    pipeline_key = models.CharField(max_length=64, db_index=True)
    job = models.ForeignKey(
        JobQueue, null=True, blank=True, on_delete=models.SET_NULL, related_name="runs"
    )
    status = models.CharField(
        max_length=16, choices=RunStatus, default=RunStatus.RUNNING, db_index=True
    )
    skip_reason = models.CharField(max_length=128, blank=True)

    started_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)

    items_in = models.IntegerField(default=0)
    items_created = models.IntegerField(default=0)
    items_updated = models.IntegerField(default=0)
    items_failed = models.IntegerField(default=0)

    keepa_tokens_used = models.IntegerField(default=0)
    llm_cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)

    error = models.TextField(blank=True)
    context = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "ejecución"
        verbose_name_plural = "ejecuciones"
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["pipeline_key", "-started_at"], name="run_pipeline_recent_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.pipeline_key} @ {self.started_at:%Y-%m-%d %H:%M} ({self.status})"


class PipelineStepRun(models.Model):
    run = models.ForeignKey(PipelineRun, related_name="steps", on_delete=models.CASCADE)
    name = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=RunStatus, default=RunStatus.RUNNING)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.IntegerField(null=True, blank=True)
    error = models.TextField(blank=True)
    context = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = "paso de ejecución"
        verbose_name_plural = "pasos de ejecución"
        ordering = ["started_at"]

    def __str__(self) -> str:
        return f"{self.run_id}/{self.name}"


class KeepaTokenLedger(models.Model):
    """Reconciled from Keepa's own response fields. We never estimate token cost."""

    at = models.DateTimeField(auto_now_add=True, db_index=True)
    endpoint = models.CharField(max_length=32)
    tokens_consumed = models.IntegerField(default=0)
    tokens_left = models.IntegerField(default=0)
    refill_in_ms = models.IntegerField(null=True, blank=True)
    refill_rate = models.IntegerField(null=True, blank=True)
    pipeline_run = models.ForeignKey(
        PipelineRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="keepa_calls"
    )

    class Meta:
        verbose_name = "movimiento de tokens Keepa"
        verbose_name_plural = "movimientos de tokens Keepa"
        ordering = ["-at"]

    def __str__(self) -> str:
        return f"{self.endpoint} −{self.tokens_consumed} → {self.tokens_left}"


class LLMCall(models.Model):
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    purpose = models.CharField(max_length=64, db_index=True)
    model = models.CharField(max_length=64)
    prompt_tokens = models.IntegerField(default=0)
    completion_tokens = models.IntegerField(default=0)
    total_tokens = models.IntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    latency_ms = models.IntegerField(null=True, blank=True)
    success = models.BooleanField(default=True)
    error = models.TextField(blank=True)
    pipeline_run = models.ForeignKey(
        PipelineRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="llm_calls"
    )

    class Meta:
        verbose_name = "llamada LLM"
        verbose_name_plural = "llamadas LLM"
        ordering = ["-at"]

    def __str__(self) -> str:
        return f"{self.purpose} · {self.model} · ${self.cost_usd}"


class NotificationLog(models.Model):
    """Backs Telegram throttling so a failing pipeline cannot spam the channel."""

    key = models.CharField(max_length=128, db_index=True)
    level = models.CharField(max_length=16, choices=NotificationLevel)
    message = models.TextField()
    sent_at = models.DateTimeField(auto_now_add=True, db_index=True)
    suppressed = models.BooleanField(default=False)

    class Meta:
        verbose_name = "notificación"
        verbose_name_plural = "notificaciones"
        ordering = ["-sent_at"]

    def __str__(self) -> str:
        return f"[{self.level}] {self.key}"
