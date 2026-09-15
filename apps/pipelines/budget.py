"""Pre-flight budget gate.

Runs *before* a pipeline starts, so a job that cannot afford its work is deferred instead of
half-executed. Deferral is cheap: no tokens, no LLM spend, no partial writes.

Token state comes from the newest ``KeepaTokenLedger`` row — i.e. from Keepa's own response
fields — projected forward with the reported refill rate. Nothing here is estimated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as dt_time

from django.conf import settings
from django.utils import timezone

from apps.pipelines.models import KeepaTokenLedger

logger = logging.getLogger(__name__)

KILL_SWITCH_RETRY_SECONDS = 300
BACKLOG_RETRY_SECONDS = 900
STALE_LEDGER_HOURS = 6


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    reason: str = ""
    retry_after_seconds: int = 0
    detail: str = ""

    def __bool__(self) -> bool:
        return self.allowed


ALLOW = Decision(allowed=True)


class BudgetGuard:
    def check(self, pipeline) -> Decision:
        for gate in (
            self._kill_switch,
            self._keepa_tokens,
            self._hydration_backlog,
            self._llm_spend,
        ):
            decision = gate(pipeline)
            if not decision.allowed:
                return decision
        return ALLOW

    # --- gates ---------------------------------------------------------------

    def _kill_switch(self, pipeline) -> Decision:
        if settings.PIPELINES_ENABLED:
            return ALLOW
        return Decision(
            allowed=False,
            reason="pipelines_disabled",
            retry_after_seconds=KILL_SWITCH_RETRY_SECONDS,
            detail="PIPELINES_ENABLED is false",
        )

    def _keepa_tokens(self, pipeline) -> Decision:
        if not pipeline.requires_keepa:
            return ALLOW

        row = KeepaTokenLedger.objects.order_by("-at").first()
        if row is None or row.at < timezone.now() - timedelta(hours=STALE_LEDGER_HOURS):
            return ALLOW  # No usable reading: let the call itself establish the baseline.

        refill_rate = max(row.refill_rate or 0, 0)
        elapsed_minutes = max((timezone.now() - row.at).total_seconds() / 60.0, 0.0)
        projected = int(row.tokens_left + elapsed_minutes * refill_rate)

        needed = pipeline.estimated_keepa_tokens + settings.KEEPA_TOKEN_RESERVE
        if projected >= needed:
            return ALLOW

        deficit = needed - projected
        wait = max(int(deficit / max(refill_rate, 1) * 60), 60)
        return Decision(
            allowed=False,
            reason="keepa_budget_exhausted",
            retry_after_seconds=wait,
            detail=f"~{projected} tokens available, need {needed}",
        )

    def _hydration_backlog(self, pipeline) -> Decision:
        """Don't generate work we cannot ingest.

        If thousands of ASINs are already waiting for Keepa hydration, spending LLM money on
        producing even more candidates is pure waste.
        """
        if not pipeline.requires_llm:
            return ALLOW

        from apps.catalog.models import Product

        backlog = Product.objects.filter(keepa_fetched_at__isnull=True).count()
        if backlog <= settings.MAX_HYDRATION_BACKLOG:
            return ALLOW
        return Decision(
            allowed=False,
            reason="hydration_backlog",
            retry_after_seconds=BACKLOG_RETRY_SECONDS,
            detail=f"{backlog} products awaiting hydration (limit {settings.MAX_HYDRATION_BACKLOG})",
        )

    def _llm_spend(self, pipeline) -> Decision:
        if not pipeline.requires_llm:
            return ALLOW

        from apps.clients.llm import LLMClient

        budget = float(settings.LLM_DAILY_BUDGET_USD)
        if budget <= 0:
            return ALLOW

        spent = float(LLMClient.spent_today_usd())
        if spent < budget:
            return ALLOW
        return Decision(
            allowed=False,
            reason="llm_budget_exhausted",
            retry_after_seconds=_seconds_until_utc_midnight(),
            detail=f"${spent:.4f} of ${budget:.2f} spent today",
        )


def _seconds_until_utc_midnight() -> int:
    now = timezone.now()
    tomorrow = datetime.combine(now.date() + timedelta(days=1), dt_time.min, tzinfo=now.tzinfo)
    return max(int((tomorrow - now).total_seconds()), 60)
