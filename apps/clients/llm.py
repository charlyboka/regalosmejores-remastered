"""OpenAI client wrapper: completions, embeddings, cost ledger and daily budget cap.

Every call writes an ``LLMCall`` row, so spend is always reconstructible from the database rather
than from provider dashboards. Before each call we check today's spend against
``LLM_DAILY_BUDGET_USD`` and raise :class:`LLMBudgetExhausted` (plus a Telegram CRITICAL) rather
than silently burning money.

Pipeline calls must pass ``json_schema`` — we never parse free text.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import time as dt_time
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from apps.clients.exceptions import LLMBudgetExhausted, LLMRequestFailed
from apps.pipelines.models import LLMCall, NotificationLevel

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ChatPricing:
    """USD per 1M tokens. Source: OpenAI price list, short-context tier.

    Our prompts are short (one product or one topic at a time), so the short-context tier applies.
    Long-context pricing is roughly 2x across the board; if a pipeline ever starts sending very
    large prompts, add a threshold here.
    """

    input: float
    cached_input: float
    output: float
    supports_temperature: bool = True


CHAT_MODELS: dict[str, ChatPricing] = {
    "gpt-6-astra": ChatPricing(input=10.00, cached_input=1.00, output=50.00),
    "gpt-5.6-sol": ChatPricing(input=4.00, cached_input=0.40, output=20.00),
    "gpt-5.6-terra": ChatPricing(input=2.00, cached_input=0.20, output=12.00),
    "gpt-5.6-luna": ChatPricing(input=0.20, cached_input=0.02, output=1.20),
}

EMBEDDING_MODELS: dict[str, float] = {
    "text-embedding-3-small": 0.02,
    "text-embedding-3-large": 0.13,
}

# Fallback used only for an unknown model name, so cost is over- rather than under-estimated.
UNKNOWN_MODEL_PRICING = ChatPricing(input=10.00, cached_input=1.00, output=50.00)
UNKNOWN_EMBEDDING_PRICE = 0.13

MAX_PARAM_STRIP_ATTEMPTS = 4


@dataclass(slots=True)
class LLMResponse:
    text: str
    parsed: dict | None
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: Decimal
    latency_ms: int


@dataclass(slots=True)
class EmbeddingResponse:
    vectors: list[list[float]] = field(default_factory=list)
    model: str = ""
    total_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    latency_ms: int = 0


class LLMClient:
    def __init__(self, *, pipeline_run=None) -> None:
        self.pipeline_run = pipeline_run
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI

            if not settings.OPENAI_API_KEY:
                raise LLMRequestFailed("OPENAI_API_KEY is not configured")
            kwargs: dict[str, Any] = {
                "api_key": settings.OPENAI_API_KEY,
                "timeout": settings.OPENAI_TIMEOUT_SECONDS,
                "max_retries": settings.OPENAI_MAX_RETRIES,
            }
            if settings.OPENAI_ORGANIZATION:
                kwargs["organization"] = settings.OPENAI_ORGANIZATION
            if settings.OPENAI_PROJECT:
                kwargs["project"] = settings.OPENAI_PROJECT
            self._client = OpenAI(**kwargs)
        return self._client

    # --- completions ---------------------------------------------------------

    def complete(
        self,
        system: str,
        user: str,
        *,
        model: str | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        json_schema: dict | None = None,
        schema_name: str = "response",
        purpose: str = "unspecified",
    ) -> LLMResponse:
        model = model or settings.OPENAI_DEFAULT_MODEL
        pricing = CHAT_MODELS.get(model, UNKNOWN_MODEL_PRICING)
        self._assert_budget(purpose)

        params: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if temperature is not None and pricing.supports_temperature:
            params["temperature"] = temperature
        if top_p is not None:
            params["top_p"] = top_p
        if max_tokens is not None:
            params["max_completion_tokens"] = max_tokens
        if json_schema is not None:
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "schema": json_schema, "strict": True},
            }

        started = time.monotonic()
        try:
            completion = self._create_with_param_fallback(params)
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            self._log_call(
                purpose=purpose,
                model=model,
                prompt_tokens=0,
                completion_tokens=0,
                cost=Decimal("0"),
                latency_ms=latency_ms,
                success=False,
                error=str(exc)[:2000],
            )
            raise LLMRequestFailed(f"{model} completion failed: {exc}") from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        usage = getattr(completion, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        cached_tokens = int(
            getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        )
        cost = self._chat_cost(pricing, prompt_tokens, cached_tokens, completion_tokens)

        text = (completion.choices[0].message.content or "").strip()
        parsed = _parse_json(text) if json_schema is not None else None
        if json_schema is not None and parsed is None:
            self._log_call(
                purpose=purpose,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost=cost,
                latency_ms=latency_ms,
                success=False,
                error="structured output was not valid JSON",
            )
            raise LLMRequestFailed(f"{model} returned invalid JSON for purpose {purpose!r}")

        self._log_call(
            purpose=purpose,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
            latency_ms=latency_ms,
            success=True,
            error="",
        )
        return LLMResponse(
            text=text,
            parsed=parsed,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
        )

    def _create_with_param_fallback(self, params: dict[str, Any]):
        """Some models reject ``temperature`` / ``top_p``. Drop what the API complains about.

        Cheaper and more robust than maintaining a per-model capability matrix by hand; the
        dropped parameter is logged so the registry can be corrected.
        """
        from openai import BadRequestError

        attempt = 0
        while True:
            try:
                return self.client.chat.completions.create(**params)
            except BadRequestError as exc:
                attempt += 1
                dropped = _unsupported_param(str(exc), params)
                if dropped is None or attempt > MAX_PARAM_STRIP_ATTEMPTS:
                    raise
                logger.warning(
                    "Model %s rejected %r; retrying without it", params.get("model"), dropped
                )
                params.pop(dropped)

    # --- embeddings ----------------------------------------------------------

    def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        dimensions: int | None = None,
        purpose: str = "embedding",
    ) -> EmbeddingResponse:
        if not texts:
            return EmbeddingResponse(model=model or settings.OPENAI_EMBEDDING_MODEL)

        model = model or settings.OPENAI_EMBEDDING_MODEL
        dimensions = dimensions or settings.OPENAI_EMBEDDING_DIMENSIONS
        self._assert_budget(purpose)

        started = time.monotonic()
        try:
            result = self.client.embeddings.create(model=model, input=texts, dimensions=dimensions)
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            self._log_call(
                purpose=purpose,
                model=model,
                prompt_tokens=0,
                completion_tokens=0,
                cost=Decimal("0"),
                latency_ms=latency_ms,
                success=False,
                error=str(exc)[:2000],
            )
            raise LLMRequestFailed(f"{model} embedding failed: {exc}") from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        total_tokens = int(getattr(getattr(result, "usage", None), "total_tokens", 0) or 0)
        price = EMBEDDING_MODELS.get(model, UNKNOWN_EMBEDDING_PRICE)
        cost = _usd(total_tokens / 1_000_000 * price)

        self._log_call(
            purpose=purpose,
            model=model,
            prompt_tokens=total_tokens,
            completion_tokens=0,
            cost=cost,
            latency_ms=latency_ms,
            success=True,
            error="",
        )
        vectors = [item.embedding for item in sorted(result.data, key=lambda d: d.index)]
        return EmbeddingResponse(
            vectors=vectors,
            model=model,
            total_tokens=total_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
        )

    def embed_one(self, text: str, **kwargs) -> list[float]:
        return self.embed([text], **kwargs).vectors[0]

    # --- budget & ledger -----------------------------------------------------

    @staticmethod
    def spent_today_usd() -> Decimal:
        start = datetime.combine(timezone.now().date(), dt_time.min, tzinfo=UTC)
        total = LLMCall.objects.filter(at__gte=start).aggregate(total=Sum("cost_usd"))["total"]
        return total or Decimal("0")

    def _assert_budget(self, purpose: str) -> None:
        budget = Decimal(str(settings.LLM_DAILY_BUDGET_USD))
        if budget <= 0:
            return
        spent = self.spent_today_usd()
        if spent < budget:
            return

        from apps.clients.telegram import notify

        notify(
            "llm_budget_exhausted",
            f"Presupuesto diario de LLM agotado: ${spent:.4f} de ${budget:.2f}. "
            f"Llamada bloqueada (proposito: {purpose}).",
            level=NotificationLevel.CRITICAL,
            throttle_minutes=180,
        )
        raise LLMBudgetExhausted(
            f"Daily LLM budget reached: ${spent:.4f} of ${budget:.2f}",
            spent_usd=float(spent),
            budget_usd=float(budget),
        )

    @staticmethod
    def _chat_cost(
        pricing: ChatPricing, prompt_tokens: int, cached_tokens: int, completion_tokens: int
    ) -> Decimal:
        fresh = max(prompt_tokens - cached_tokens, 0)
        dollars = (
            fresh / 1_000_000 * pricing.input
            + cached_tokens / 1_000_000 * pricing.cached_input
            + completion_tokens / 1_000_000 * pricing.output
        )
        return _usd(dollars)

    def _log_call(
        self,
        *,
        purpose: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost: Decimal,
        latency_ms: int,
        success: bool,
        error: str,
    ) -> None:
        LLMCall.objects.create(
            purpose=purpose,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            success=success,
            error=error,
            pipeline_run=self.pipeline_run,
        )


# --- module helpers ----------------------------------------------------------


def _usd(value: float) -> Decimal:
    """Quantised to 6 decimals to match ``LLMCall.cost_usd``."""
    return Decimal(str(round(value, 6)))


def _parse_json(text: str) -> dict | None:
    import json

    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _unsupported_param(message: str, params: dict) -> str | None:
    lowered = message.lower()
    if "unsupported" not in lowered and "not supported" not in lowered:
        return None
    for name in ("temperature", "top_p", "max_completion_tokens", "response_format"):
        if name in params and name in lowered:
            return name
    return None
