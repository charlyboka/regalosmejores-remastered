"""Live smoke test for the three external clients.

Makes one real call to each provider and prints what came back, so a misconfigured key or a
changed API shape is caught here instead of halfway through a pipeline.

    uv run python manage.py ping_clients
    uv run python manage.py ping_clients --skip-telegram
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.clients.exceptions import ClientError
from apps.clients.keepa import KeepaClient
from apps.clients.llm import LLMClient
from apps.clients.telegram import TelegramClient
from apps.pipelines.models import NotificationLevel

# Minimal Product Finder selection. Keepa enforces perPage >= 50, so that is the floor even
# though we only hydrate the first couple of ASINs.
PROBE_SELECTION = {
    "productType": [0],
    "current_SALES_gte": 1,
    "current_SALES_lte": 30000,
    "perPage": 50,
    "page": 0,
}


class Command(BaseCommand):
    help = "Verify Keepa, OpenAI and Telegram connectivity with one live call each."

    def add_arguments(self, parser):
        parser.add_argument("--skip-keepa", action="store_true")
        parser.add_argument("--skip-llm", action="store_true")
        parser.add_argument("--skip-telegram", action="store_true")

    def handle(self, *args, **options):
        failures: list[str] = []

        for name, skip, check in (
            ("Keepa", options["skip_keepa"], self._check_keepa),
            ("OpenAI", options["skip_llm"], self._check_llm),
            ("Telegram", options["skip_telegram"], self._check_telegram),
        ):
            self.stdout.write(self.style.MIGRATE_HEADING(f"\n== {name} =="))
            if skip:
                self.stdout.write("  skipped")
                continue
            try:
                check()
            except (ClientError, Exception) as exc:  # noqa: B014 - report, never crash the command
                failures.append(name)
                self.stdout.write(self.style.ERROR(f"  FAILED: {type(exc).__name__}: {exc}"))

        self.stdout.write("")
        if failures:
            self.stdout.write(self.style.ERROR(f"FAILED: {', '.join(failures)}"))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("All clients OK"))

    # --- checks --------------------------------------------------------------

    def _check_keepa(self) -> None:
        client = KeepaClient()
        status = client.token_status()
        self.stdout.write(
            f"  tokens left: {status.tokens_left}  refill rate: {status.refill_rate}/min  "
            f"next refill in: {status.refill_in_ms} ms"
        )

        asins, total = client.find_products(PROBE_SELECTION)
        self.stdout.write(f"  product finder: {len(asins)} asins of {total} total -> {asins[:5]}")
        if not asins:
            self.stdout.write(self.style.WARNING("  no asins returned; skipping hydration"))
            return

        products = client.get_products(asins[:2])
        self.stdout.write(f"  hydrated {len(products)} product(s)")
        for product in products:
            self.stdout.write(
                f"    {product.asin}  {product.title[:60]!r}\n"
                f"      brand={product.brand!r} rating={product.rating} "
                f"reviews={product.review_count} rank={product.sales_rank} "
                f"images={len(product.image_urls)} features={len(product.features)} "
                f"price_cents={product.price_cents}"
            )
        self.stdout.write(f"  tokens left after: {client.token_status().tokens_left}")

    def _check_llm(self) -> None:
        client = LLMClient()
        self.stdout.write(f"  spent today: ${client.spent_today_usd():.6f}")

        response = client.complete(
            system="Eres un asistente que responde solo en JSON valido.",
            user="Devuelve el campo ok con el valor true y el campo idioma con el valor espanol.",
            json_schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}, "idioma": {"type": "string"}},
                "required": ["ok", "idioma"],
                "additionalProperties": False,
            },
            schema_name="ping",
            purpose="ping",
        )
        self.stdout.write(
            f"  {response.model}: parsed={response.parsed} "
            f"tokens={response.prompt_tokens}+{response.completion_tokens} "
            f"cost=${response.cost_usd} latency={response.latency_ms} ms"
        )

        embedding = client.embed(["regalo original para mi padre aficionado a la jardineria"])
        self.stdout.write(
            f"  {embedding.model}: dims={len(embedding.vectors[0])} "
            f"tokens={embedding.total_tokens} cost=${embedding.cost_usd} "
            f"latency={embedding.latency_ms} ms"
        )

    def _check_telegram(self) -> None:
        client = TelegramClient()
        if not client.is_configured:
            raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL_ID not configured")
        sent = client.notify(
            "ping_clients",
            "Prueba de conectividad desde <code>ping_clients</code>.",
            level=NotificationLevel.INFO,
            throttle_minutes=0,
        )
        if not sent:
            raise RuntimeError("sendMessage did not succeed; check the log above")
        self.stdout.write("  message delivered to the channel")
