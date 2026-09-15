"""Exceptions raised by the external API clients.

Pipelines catch these to decide between *defer* (budget/rate limits — retry later) and
*fail* (auth/validation — needs a human).
"""


class ClientError(Exception):
    """Base class for every client-level failure."""


# --- Keepa -------------------------------------------------------------------


class KeepaError(ClientError):
    pass


class KeepaAuthError(KeepaError):
    """Bad or missing API key. Not retryable — fires a Telegram ERROR."""


class KeepaRequestFailed(KeepaError):
    """Transport or server error that survived all retries."""


class KeepaBudgetExhausted(KeepaError):
    """Not enough tokens left. Retryable once the bucket refills."""

    def __init__(self, message: str, *, retry_after_seconds: int = 60) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


# --- LLM ---------------------------------------------------------------------


class LLMError(ClientError):
    pass


class LLMRequestFailed(LLMError):
    """Provider error that survived the SDK's own retries."""


class LLMBudgetExhausted(LLMError):
    """Daily spend cap (LLM_DAILY_BUDGET_USD) reached. Retryable tomorrow."""

    def __init__(self, message: str, *, spent_usd: float, budget_usd: float) -> None:
        super().__init__(message)
        self.spent_usd = spent_usd
        self.budget_usd = budget_usd
