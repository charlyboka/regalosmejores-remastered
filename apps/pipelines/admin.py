from django.contrib import admin
from django.utils import timezone

from .models import (
    JobQueue,
    JobStatus,
    KeepaTokenLedger,
    LLMCall,
    NotificationLog,
    PipelineRun,
    PipelineSchedule,
    PipelineStepRun,
)


@admin.register(PipelineSchedule)
class PipelineScheduleAdmin(admin.ModelAdmin):
    list_display = (
        "pipeline_key",
        "enabled",
        "cron",
        "max_per_run",
        "next_run_at",
        "last_run_at",
        "consecutive_failures",
    )
    list_filter = ("enabled",)
    search_fields = ("pipeline_key",)
    list_editable = ("enabled", "cron", "max_per_run")
    ordering = ("pipeline_key",)
    actions = ("enable_schedules", "disable_schedules", "run_now")

    @admin.action(description="Activar")
    def enable_schedules(self, request, queryset):
        updated = queryset.update(enabled=True)
        self.message_user(request, f"{updated} programaciones activadas.")

    @admin.action(description="Desactivar")
    def disable_schedules(self, request, queryset):
        updated = queryset.update(enabled=False)
        self.message_user(request, f"{updated} programaciones desactivadas.")

    @admin.action(description="Ejecutar en el próximo tick")
    def run_now(self, request, queryset):
        updated = queryset.update(next_run_at=timezone.now())
        self.message_user(request, f"{updated} programaciones marcadas para ejecución inmediata.")


@admin.register(JobQueue)
class JobQueueAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "pipeline_key",
        "status",
        "priority",
        "available_at",
        "attempts",
        "max_attempts",
        "locked_by",
        "updated_at",
    )
    list_filter = ("status", "pipeline_key")
    search_fields = ("pipeline_key", "dedupe_key", "last_error")
    ordering = ("priority", "available_at")
    list_per_page = 100
    readonly_fields = ("created_at", "updated_at", "locked_by", "locked_at")
    actions = ("retry_jobs", "cancel_jobs")

    @admin.action(description="Reintentar ahora (reinicia intentos)")
    def retry_jobs(self, request, queryset):
        updated = queryset.update(
            status=JobStatus.QUEUED,
            attempts=0,
            available_at=timezone.now(),
            locked_by="",
            locked_at=None,
            last_error="",
        )
        self.message_user(request, f"{updated} trabajos reencolados.")

    @admin.action(description="Cancelar (marcar como FAILED)")
    def cancel_jobs(self, request, queryset):
        updated = queryset.update(
            status=JobStatus.FAILED, last_error="Cancelado manualmente en Admin"
        )
        self.message_user(request, f"{updated} trabajos cancelados.")


class PipelineStepRunInline(admin.TabularInline):
    model = PipelineStepRun
    extra = 0
    fields = ("name", "status", "duration_ms", "error")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(PipelineRun)
class PipelineRunAdmin(admin.ModelAdmin):
    list_display = (
        "started_at",
        "pipeline_key",
        "status",
        "duration_ms",
        "items_in",
        "items_created",
        "items_updated",
        "items_failed",
        "keepa_tokens_used",
        "llm_cost_usd",
        "skip_reason",
    )
    list_filter = ("status", "pipeline_key")
    search_fields = ("pipeline_key", "error", "skip_reason")
    date_hierarchy = "started_at"
    ordering = ("-started_at",)
    list_per_page = 100
    inlines = [PipelineStepRunInline]
    readonly_fields = tuple(f.name for f in PipelineRun._meta.fields if f.name != "id")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(KeepaTokenLedger)
class KeepaTokenLedgerAdmin(admin.ModelAdmin):
    list_display = (
        "at",
        "endpoint",
        "tokens_consumed",
        "tokens_left",
        "refill_rate",
        "refill_in_ms",
        "pipeline_run",
    )
    list_filter = ("endpoint",)
    date_hierarchy = "at"
    ordering = ("-at",)
    list_per_page = 100
    readonly_fields = tuple(f.name for f in KeepaTokenLedger._meta.fields if f.name != "id")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(LLMCall)
class LLMCallAdmin(admin.ModelAdmin):
    list_display = (
        "at",
        "purpose",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "cost_usd",
        "latency_ms",
        "success",
    )
    list_filter = ("purpose", "model", "success")
    search_fields = ("purpose", "model", "error")
    date_hierarchy = "at"
    ordering = ("-at",)
    list_per_page = 100
    readonly_fields = tuple(f.name for f in LLMCall._meta.fields if f.name != "id")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(NotificationLog)
class NotificationLogAdmin(admin.ModelAdmin):
    list_display = ("sent_at", "level", "key", "suppressed", "short_message")
    list_filter = ("level", "suppressed")
    search_fields = ("key", "message")
    date_hierarchy = "sent_at"
    ordering = ("-sent_at",)
    list_per_page = 100
    readonly_fields = tuple(f.name for f in NotificationLog._meta.fields if f.name != "id")

    @admin.display(description="Mensaje")
    def short_message(self, obj: NotificationLog) -> str:
        return obj.message[:100]

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False
