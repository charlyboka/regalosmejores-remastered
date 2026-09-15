"""Harness for tuning the ranking weights: runs many queries and summarises the outcome.

Not a test suite — there are no tests in this project. It exists so that after changing a weight
in Admin you can see, in one screen, whether anything regressed: zero-result queries, duplicated
variation families, brand concentration, latency.
"""

from __future__ import annotations

import statistics
import time

from django.core.management.base import BaseCommand

from apps.search.engine import search
from apps.search.models import RankingConfig

#: Written to sound like real shoppers, not like keyword strings: accents, typos, long phrases,
#: vague intent. If the engine only survives clean keywords it does not survive production.
QUERIES: list[str] = [
    "regalo para mi madre que le gusta la jardinería",
    "qué le regalo a mi padre por su cumpleaños",
    "regalo original para mi pareja",
    "regalo de navidad para un adolescente al que le gusta la tecnología",
    "algo para una amiga que acaba de mudarse",
    "regalo barato para el amigo invisible de la oficina",
    "juguete para un niño de 5 años",
    "regalo para un bebé recién nacido",
    "regalo para mi abuela",
    "regalo para mi hermano que juega a videojuegos",
    "regalo para alguien que le encanta cocinar",
    "detalle para una profesora",
    "regalo de aniversario para mi mujer",
    "regalo para un amante del café",
    "ideas de regalo para runners",
    "regalo para mi jefe",
    "regalo para un niño que le gustan las manualidades",
    "regalo para una persona que trabaja desde casa",
    "regalo para mi sobrino de tres años",
    "qué regalar a un adolescente de 15 años",
    "regalo para alguien que tiene un perro",
    "regalo para un amigo al que le gusta el deporte",
    "regalo de comunión para un niño",
    "regalo para mi suegra",
    "regalo tecnológico por menos de 50 euros",
    "regalo para una persona muy creativa",
    "regalo para alguien que viaja mucho",
    "regalo para mi mejor amiga por su cumpleaños",
    "regalo para un fan de lego",
    "regalo para una pareja que se acaba de casar",
    "regalo utíl para casa",  # typo on purpose
    "regalo para niños pequeños que no sea un juguete",
    "regalo para un adolescente que le gusta dibujar",
    "regalo para mi padre que ya lo tiene todo",
    "algo bonito para una madre primeriza",
    "regalo para alguien que le gusta la música",
    "regalo de jubilación",
    "regalo para un apasionado de los coches",
    "regalo para mi hija de diez años",
    "regalo para alguien que le gusta leer",
]


class Command(BaseCommand):
    help = "Ejecuta un lote de consultas reales y resume la calidad del ranking."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=12)
        parser.add_argument("--show", type=int, default=3, help="Resultados a mostrar por consulta")
        parser.add_argument("--quiet", action="store_true", help="Solo el resumen final.")

    def handle(self, *args, **options):
        config = RankingConfig.load()
        limit, show = options["limit"], options["show"]

        latencies: list[int] = []
        zero_results: list[str] = []
        duplicate_variations: list[str] = []
        counts: list[int] = []

        for query in QUERIES:
            started = time.monotonic()
            response = search(query, limit=limit, use_cache=False)
            latencies.append(int((time.monotonic() - started) * 1000))
            counts.append(len(response.results))

            groups = [r.product.variation_group_key or r.product.asin for r in response.results]
            if len(groups) != len(set(groups)):
                duplicate_variations.append(query)
            if not response.results:
                zero_results.append(query)

            if not options["quiet"]:
                brands = {(r.product.brand or "-") for r in response.results}
                self.stdout.write(
                    self.style.HTTP_INFO(
                        f"\n{query}\n  {len(response.results)} resultados | "
                        f"{len(brands)} marcas | {latencies[-1]} ms"
                    )
                )
                for position, result in enumerate(response.results[:show], start=1):
                    self.stdout.write(
                        f"   {position}. {_ascii(result.product.title)[:66]}  ({result.score:.3f})"
                    )

        self.stdout.write(self.style.HTTP_INFO("\n" + "=" * 72))
        self.stdout.write(f"Consultas            {len(QUERIES)}")
        self.stdout.write(
            f"Resultados           media {statistics.mean(counts):.1f} | "
            f"mínimo {min(counts)} | máximo {max(counts)}"
        )
        self.stdout.write(
            f"Latencia             p50 {statistics.median(latencies):.0f} ms | "
            f"p95 {sorted(latencies)[int(len(latencies) * 0.95) - 1]} ms | "
            f"máx {max(latencies)} ms"
        )
        self.stdout.write(
            f"  pesos: sem={config.w_semantic} lex={config.w_lexical} qual={config.w_quality} "
            f"ctr={config.w_ctr} fresh={config.w_freshness} | min_sim={config.min_facet_similarity}"
        )

        if duplicate_variations:
            self.stdout.write(
                self.style.ERROR(
                    f"\nFAMILIAS DUPLICADAS en {len(duplicate_variations)} consultas: "
                    f"{duplicate_variations[:5]}"
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("\nSin familias de variantes duplicadas."))

        if zero_results:
            self.stdout.write(
                self.style.WARNING(f"Sin resultados ({len(zero_results)}): {zero_results}")
            )
        else:
            self.stdout.write(self.style.SUCCESS("Todas las consultas devolvieron resultados."))


def _ascii(text: str) -> str:
    """The Windows console is cp1252; tuning output should never die on a título."""
    return text.encode("ascii", "replace").decode("ascii")
