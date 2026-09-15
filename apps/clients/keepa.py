"""Keepa API client.

Design notes
------------
* **Self-calibrating budget.** Every Keepa response carries ``tokensLeft``, ``tokensConsumed``,
  ``refillIn`` and ``refillRate``. We persist all four to ``KeepaTokenLedger`` on every call and
  project the current balance from the most recent row, so token costs are never hardcoded.
* **Normalisation at the boundary.** Keepa's CSV index numbers stay in this module. The rest of
  the codebase only ever sees :class:`KeepaProduct`.
* ``price_cents`` is captured solely to derive ``Product.price_band``. Prices are never displayed
  (see blueprint 2 and 8).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from django.conf import settings
from django.utils import timezone

from apps.clients.exceptions import KeepaAuthError, KeepaBudgetExhausted, KeepaRequestFailed

logger = logging.getLogger(__name__)

BASE_URL = "https://api.keepa.com"
IMAGE_BASE_URL = "https://m.media-amazon.com/images/I"

# Keepa stores time as "minutes since 2011-01-01". This is the documented offset.
KEEPA_EPOCH_MINUTES = 21_564_000

# Indices into stats["current"] / csv. Only the ones we actually read are named.
CSV_AMAZON = 0
CSV_NEW = 1
CSV_SALES = 3
CSV_RATING = 16
CSV_REVIEW_COUNT = 17
CSV_BUY_BOX = 18

# Preference order when resolving "what does this cost right now".
PRICE_SOURCES = (CSV_BUY_BOX, CSV_NEW, CSV_AMAZON)

MAX_ASINS_PER_PRODUCT_CALL = 100
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_ATTEMPTS = 4

# Pre-flight estimates only — the real cost is read back from every response and persisted, so
# these just size the guard that stops us dipping below KEEPA_TOKEN_RESERVE. Measured against
# amazon.es: /product with stats+rating+buybox ~3.5 tokens per ASIN, /query ~11 tokens per call.
TOKENS_PER_ASIN_ESTIMATE = 4
TOKENS_PER_FINDER_CALL_ESTIMATE = 12


@dataclass(slots=True)
class TokenStatus:
    """Snapshot of the Keepa token bucket, taken from any response."""

    tokens_left: int
    tokens_consumed: int
    refill_in_ms: int
    refill_rate: int
    at: Any = None

    def projected_tokens(self, *, now=None) -> int:
        """Balance now, extrapolated from when this snapshot was taken."""
        if self.at is None:
            return self.tokens_left
        now = now or timezone.now()
        minutes = max((now - self.at).total_seconds() / 60.0, 0.0)
        return int(self.tokens_left + minutes * self.refill_rate)


@dataclass(slots=True)
class KeepaProduct:
    """Normalised Keepa product. The only shape the rest of the codebase sees."""

    asin: str
    domain_id: int
    title: str = ""
    brand: str = ""
    manufacturer: str = ""
    model: str = ""
    parent_asin: str = ""
    product_group: str = ""
    root_category_id: int | None = None
    category_ids: list[int] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    description_text: str = ""
    image_urls: list[str] = field(default_factory=list)
    rating: float | None = None
    review_count: int | None = None
    sales_rank: int | None = None
    price_cents: int | None = None
    is_adult: bool = False
    listed_since: Any = None
    variation_group_key: str = ""
    binding: str = ""
    payload_hash: str = ""

    @property
    def has_minimum_content(self) -> bool:
        """Cheap guard used by ingestion before spending LLM budget on a product."""
        return bool(self.title) and bool(self.image_urls)


class KeepaClient:
    """Thin, typed wrapper over the Keepa HTTP API. No business logic lives here."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        domain_id: int | None = None,
        token_reserve: int | None = None,
        timeout: int | None = None,
        pipeline_run=None,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.KEEPA_API_KEY
        self.domain_id = domain_id if domain_id is not None else settings.KEEPA_DOMAIN_ID
        self.token_reserve = (
            token_reserve if token_reserve is not None else settings.KEEPA_TOKEN_RESERVE
        )
        self.timeout = timeout if timeout is not None else settings.KEEPA_REQUEST_TIMEOUT_SECONDS
        self.pipeline_run = pipeline_run
        self._last_status: TokenStatus | None = None

    # --- public API ----------------------------------------------------------

    def token_status(self) -> TokenStatus:
        """Free status probe (the ``/token`` endpoint does not consume tokens)."""
        payload = self._request("token", {}, expected_cost=0)
        return self._record_tokens("token", payload, expected_cost=0)

    def get_products(self, asins: list[str], *, stats_days: int = 30) -> list[KeepaProduct]:
        """Hydrate up to 100 ASINs per call. Returns only the ones Keepa actually knows."""
        results: list[KeepaProduct] = []
        for batch in _chunks(asins, MAX_ASINS_PER_PRODUCT_CALL):
            params = {
                "asin": ",".join(batch),
                "stats": stats_days,
                "history": 0,
                "buybox": 1,
                "rating": 1,
            }
            cost = len(batch) * TOKENS_PER_ASIN_ESTIMATE
            payload = self._request("product", params, expected_cost=cost)
            self._record_tokens("product", payload, expected_cost=cost)
            for raw in payload.get("products") or []:
                if raw.get("asin"):
                    results.append(self._normalise(raw))
        return results

    def find_products(self, selection: dict[str, Any]) -> tuple[list[str], int]:
        """Product Finder. Harvests ASINs in bulk — the cheapest discovery path.

        Returns ``(asin_list, total_results)``.
        """
        params = {"selection": json.dumps(selection, separators=(",", ":"))}
        payload = self._request("query", params, expected_cost=TOKENS_PER_FINDER_CALL_ESTIMATE)
        self._record_tokens("query", payload, expected_cost=TOKENS_PER_FINDER_CALL_ESTIMATE)
        return list(payload.get("asinList") or []), int(payload.get("totalResults") or 0)

    def search_products(self, term: str, *, page: int = 0) -> list[KeepaProduct]:
        """Keyword search. More expensive per ASIN than Product Finder — use sparingly."""
        params = {"type": "product", "term": term, "page": page, "stats": 30, "history": 0}
        payload = self._request("search", params, expected_cost=TOKENS_PER_FINDER_CALL_ESTIMATE)
        self._record_tokens("search", payload, expected_cost=TOKENS_PER_FINDER_CALL_ESTIMATE)
        return [self._normalise(raw) for raw in (payload.get("products") or []) if raw.get("asin")]

    # --- transport -----------------------------------------------------------

    def _request(self, endpoint: str, params: dict[str, Any], *, expected_cost: int) -> dict:
        if not self.api_key:
            raise KeepaAuthError("KEEPA_API_KEY is not configured")

        if expected_cost:
            self._assert_budget(expected_cost)

        query = {"key": self.api_key, "domain": self.domain_id, **params}
        url = f"{BASE_URL}/{endpoint}"
        last_error: Exception | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = httpx.get(url, params=query, timeout=self.timeout)
            except httpx.HTTPError as exc:
                last_error = exc
                logger.warning("Keepa %s transport error (attempt %s): %s", endpoint, attempt, exc)
                self._sleep_backoff(attempt)
                continue

            if response.status_code in (401, 403):
                raise KeepaAuthError(f"Keepa rejected the API key (HTTP {response.status_code})")

            if response.status_code == 429:
                # Keepa uses 429 for "not enough tokens", which is a budget condition, not a bug.
                retry_after = self._retry_after_seconds(response)
                raise KeepaBudgetExhausted(
                    f"Keepa returned 429 on /{endpoint}", retry_after_seconds=retry_after
                )

            if response.status_code in RETRYABLE_STATUS:
                last_error = KeepaRequestFailed(f"HTTP {response.status_code} on /{endpoint}")
                logger.warning(
                    "Keepa %s HTTP %s (attempt %s)", endpoint, response.status_code, attempt
                )
                self._sleep_backoff(attempt)
                continue

            if response.status_code >= 400:
                raise KeepaRequestFailed(
                    f"Keepa /{endpoint} failed: HTTP {response.status_code} {response.text[:300]}"
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise KeepaRequestFailed(f"Keepa /{endpoint} returned non-JSON") from exc

            if error := payload.get("error"):
                raise KeepaRequestFailed(f"Keepa /{endpoint} error: {error}")
            return payload

        raise KeepaRequestFailed(
            f"Keepa /{endpoint} failed after {MAX_ATTEMPTS} attempts"
        ) from last_error

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> int:
        raw = response.headers.get("Retry-After")
        if raw and raw.isdigit():
            return min(int(raw), 3600)
        return 60

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        time.sleep(min(2**attempt, 30))

    # --- budget --------------------------------------------------------------

    def _assert_budget(self, expected_cost: int) -> None:
        status = self._latest_status()
        if status is None:
            return  # No history yet — let the first call establish the baseline.

        projected = status.projected_tokens()
        if projected - expected_cost >= self.token_reserve:
            return

        deficit = self.token_reserve + expected_cost - projected
        refill_rate = max(status.refill_rate, 1)
        wait_seconds = max(int(deficit / refill_rate * 60), 30)
        raise KeepaBudgetExhausted(
            f"Keepa tokens too low: ~{projected} left, need {expected_cost} "
            f"plus a reserve of {self.token_reserve}",
            retry_after_seconds=wait_seconds,
        )

    def _latest_status(self) -> TokenStatus | None:
        if self._last_status is not None:
            return self._last_status
        from apps.pipelines.models import KeepaTokenLedger

        row = KeepaTokenLedger.objects.order_by("-at").first()
        if row is None:
            return None
        # A stale snapshot tells us nothing useful; treat it as unknown.
        if row.at < timezone.now() - timedelta(hours=6):
            return None
        self._last_status = TokenStatus(
            tokens_left=row.tokens_left,
            tokens_consumed=row.tokens_consumed,
            refill_in_ms=row.refill_in_ms,
            refill_rate=row.refill_rate,
            at=row.at,
        )
        return self._last_status

    def _record_tokens(self, endpoint: str, payload: dict, *, expected_cost: int) -> TokenStatus:
        from apps.pipelines.models import KeepaTokenLedger

        status = TokenStatus(
            tokens_left=int(payload.get("tokensLeft") or 0),
            tokens_consumed=int(payload.get("tokensConsumed") or expected_cost),
            refill_in_ms=int(payload.get("refillIn") or 0),
            refill_rate=int(payload.get("refillRate") or 0),
            at=timezone.now(),
        )
        self._last_status = status
        KeepaTokenLedger.objects.create(
            at=status.at,
            endpoint=endpoint,
            tokens_consumed=status.tokens_consumed,
            tokens_left=status.tokens_left,
            refill_in_ms=status.refill_in_ms,
            refill_rate=status.refill_rate,
            pipeline_run=self.pipeline_run,
        )
        return status

    # --- normalisation -------------------------------------------------------

    def _normalise(self, raw: dict) -> KeepaProduct:
        stats_current = ((raw.get("stats") or {}).get("current")) or []

        return KeepaProduct(
            asin=raw["asin"],
            domain_id=int(raw.get("domainId") or self.domain_id),
            title=_clean(raw.get("title")),
            brand=_clean(raw.get("brand")),
            manufacturer=_clean(raw.get("manufacturer")),
            model=_clean(raw.get("model")),
            parent_asin=_clean(raw.get("parentAsin")),
            # Keepa stopped populating `productGroup` — it is null on every response now. `type`
            # (PHYSICAL_MOVIE, DIGITAL_EBOOK, …) carries the same meaning and is still filled in.
            product_group=_clean(raw.get("type") or raw.get("productGroup")),
            root_category_id=_positive(raw.get("rootCategory")),
            category_ids=[int(c) for c in (raw.get("categories") or []) if c],
            features=[f.strip() for f in (raw.get("features") or []) if f and f.strip()],
            description_text=_clean(raw.get("description")),
            image_urls=_image_urls(raw.get("images")),
            rating=_rating(stats_current),
            review_count=_review_count(raw, stats_current),
            sales_rank=_stat(stats_current, CSV_SALES),
            price_cents=_price_cents(stats_current),
            is_adult=bool(raw.get("isAdultProduct")),
            listed_since=_keepa_time(raw.get("listedSince")),
            variation_group_key=_variation_key(raw),
            binding=_clean(raw.get("binding")),
            payload_hash=_content_hash(raw),
        )


# --- module helpers ----------------------------------------------------------


def _chunks(items: list[str], size: int):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _positive(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _stat(current: list, index: int) -> int | None:
    """Keepa encodes 'no data' as -1."""
    if index >= len(current):
        return None
    value = current[index]
    return int(value) if isinstance(value, int) and value >= 0 else None


def _rating(current: list) -> float | None:
    raw = _stat(current, CSV_RATING)
    return round(raw / 10, 1) if raw is not None else None


def _price_cents(current: list) -> int | None:
    for index in PRICE_SOURCES:
        value = _stat(current, index)
        if value:
            return value
    return None


def _image_urls(images: Any) -> list[str]:
    """Keepa returns ``images`` as objects: ``{"l": "<file>.jpg", "variant": "MAIN", ...}``.

    The MAIN variant is hoisted to the front so ``Product.primary_image_url`` is the hero shot.
    """
    if not isinstance(images, list):
        return []
    ordered = sorted(images, key=lambda i: 0 if i.get("variant") == "MAIN" else 1)
    urls = []
    for image in ordered:
        name = image.get("l") or image.get("m")
        if isinstance(name, str) and name.strip():
            urls.append(f"{IMAGE_BASE_URL}/{name.strip()}")
    return urls


def _review_count(raw: dict, current: list) -> int | None:
    reviews = raw.get("reviews") or {}
    for value in (reviews.get("ratingCount"), reviews.get("reviewCount")):
        if isinstance(value, int) and value >= 0:
            return value
    return _stat(current, CSV_REVIEW_COUNT)


def _keepa_time(minutes: Any):
    if not isinstance(minutes, int) or minutes <= 0:
        return None
    return datetime.fromtimestamp((minutes + KEEPA_EPOCH_MINUTES) * 60, tz=UTC)


def _variation_key(raw: dict) -> str:
    """Groups colour/size variants so ranking can show one entry per real product."""
    parent = _clean(raw.get("parentAsin"))
    if parent:
        return parent
    variations = raw.get("variations")
    if isinstance(variations, list) and variations:
        asins = sorted(_clean(v.get("asin")) for v in variations if isinstance(v, dict))
        if asins and asins[0]:
            return asins[0]
    return _clean(raw.get("asin"))


def _content_hash(raw: dict) -> str:
    """Hash of the *content* fields only.

    Deliberately excludes price, rank and rating so that a re-fetch of an unchanged listing does
    not invalidate its enrichment (facets and embeddings are derived from content alone).
    """
    content = {
        "title": raw.get("title"),
        "brand": raw.get("brand"),
        "features": raw.get("features"),
        "description": raw.get("description"),
        "images": [i.get("l") for i in (raw.get("images") or []) if isinstance(i, dict)],
        "categories": raw.get("categories"),
    }
    blob = json.dumps(content, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
