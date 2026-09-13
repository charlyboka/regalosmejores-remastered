"""`manage.py search "<query>"` — run the real search pipeline and show the score breakdown.

The tuning tool for Phase 6: change a weight in Admin, re-run, see what moved.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from apps.search.engine import search
from apps.search.models import RankingConfig


class Command(BaseCommand):
    help = "Busca productos y muestra el desglose de puntuación."

    def add_arguments(self, parser):
        parser.add_argument("query", nargs="*", help="Texto de la consulta.")
        parser.add_argument("--limit", type=int, default=10)
        parser.add_argument("--slots", default="", help='JSON, p. ej. \'{"recipients":["madre"]}\'')
        parser.add_argument("--no-cache", action="store_true", help="Ignora la caché.")
        parser.add_argument("--explain", action="store_true", help="Muestra la faceta que casó.")

    def handle(self, *args, **options):
        text = " ".join(options["query"]).strip()
        slots = json.loads(options["slots"]) if options["slots"] else None
        if not text and not slots:
            self.stderr.write("Indica una consulta o --slots.")
            return

        config = RankingConfig.load()
        response = search(
            text, slots=slots, limit=options["limit"], use_cache=not options["no_cache"]
        )

        source = "cache" if response.served_from_cache else "en vivo"
        self.stdout.write(
            self.style.HTTP_INFO(
                f'\n"{response.normalized}"  |  {source}  |  {response.latency_ms} ms '
                f"(embed {response.embed_ms} ms)  |  {response.total_candidates} candidatos  |  "
                f"{len(response.results)} resultados"
            )
        )
        self.stdout.write(
            f"  pesos: sem={config.w_semantic} lex={config.w_lexical} qual={config.w_quality} "
            f"ctr={config.w_ctr} fresh={config.w_freshness} | min_sim={config.min_facet_similarity}"
        )

        if not response.results:
            self.stdout.write(self.style.WARNING("\n  Sin resultados."))
            return

        for position, result in enumerate(response.results, start=1):
            product = result.product
            self.stdout.write(
                f"\n{position:>3}. {product.title[:74]}"
                f"\n     {product.asin} | {product.brand or '-'} | {product.price_band or '-'} "
                f"| grupo={product.variation_group_key}"
                f"\n     {result.explain()}  [{'+'.join(sorted(result.matched_by))}]"
            )
            if options["explain"] and result.facet_text:
                self.stdout.write(f'     > "{result.facet_text}"')
