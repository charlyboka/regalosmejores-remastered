"""Turning a product into synthetic gift queries.

The single biggest quality lever in the system (§6.1). We never embed a product title: a user
types "algo para mi hermana que se acaba de mudar" and a title says "Juego de 6 vasos de cristal
350 ml" — different semantic spaces, bad matches. Instead the LLM writes the *queries this product
answers*, we embed those, and at request time we compare query to query.

This module owns the prompt, the JSON schema and the validation. The pipeline owns the I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from apps.catalog import categories, vocabularies

#: How many synthetic queries we ask for per product. More facets means better recall but linearly
#: more storage: each is a halfvec(512) row (~1 KB) plus its HNSW index entry.
DEFAULT_MIN_QUERIES = 6
DEFAULT_MAX_QUERIES = 8

#: Tags per facet. A query that claims five occasions is a query that means nothing.
MAX_OCCASIONS_PER_FACET = 3
MAX_RECIPIENTS_PER_FACET = 3
MAX_INTERESTS_PER_FACET = 4

#: A query shorter than this is too generic to retrieve on ("regalo bonito").
MIN_QUERY_WORDS = 4
MAX_QUERY_WORDS = 14

#: The summary facet is real retrieval surface, but a description matches a user query less well
#: than a query does, so it carries less weight in ranking.
SUMMARY_WEIGHT = 0.6

#: Truncation limits for the prompt. Amazon descriptions can be thousands of words of marketing;
#: past a couple of hundred they add cost without adding signal.
MAX_FEATURES = 8
MAX_FEATURE_CHARS = 200
MAX_DESCRIPTION_CHARS = 900


SYSTEM_PROMPT = """\
Eres un experto en regalos que trabaja para un buscador español de ideas de regalo.

Tu tarea: dado un producto de Amazon, escribir las consultas de búsqueda reales que una persona
escribiría cuando busca un regalo y para las que ESTE producto sería una buena respuesta.

Reglas de las consultas:
- Escríbelas en español natural, en minúsculas, como las teclearía una persona real.
- Entre {min_q} y {max_q} consultas, todas distintas entre sí.
- Cada una entre {min_w} y {max_w} palabras.
- Varía el ángulo: destinatario, ocasión, afición, problema que resuelve, momento de la vida.
  No repitas la misma estructura en todas.
- NO copies el título ni lo parafrasees. Una consulta no es una descripción del producto.
- NO menciones marcas, salvo que la marca sea el motivo real por el que alguien lo regala
  (por ejemplo LEGO o Nintendo, nunca una marca genérica de la que nadie ha oído hablar).
- NO menciones precios, ni euros, ni "barato" o "caro".
- NO inventes características que no aparecen en los datos del producto.
- Si el producto no es un buen regalo para alguien, no fuerces la consulta: es mejor que sean
  específicas y honestas que muchas y vagas.

Reglas del resumen:
- Una sola frase, máximo 30 palabras, que explique por qué funciona como regalo y para quién.
- Sin superlativos publicitarios ni signos de exclamación.

Reglas de las etiquetas:
- Usa SOLO las claves permitidas. Etiqueta cada consulta con lo que esa consulta implica,
  no con todo lo que el producto podría ser.
- Si una consulta no implica ninguna ocasión concreta, deja la lista vacía.
- Prefiere pocas etiquetas correctas a muchas aproximadas.
- Fíjate en los tramos de edad: son excluyentes, elige el que de verdad corresponda.

CLAVES PERMITIDAS (clave = significado)

Ocasiones:
{occasions}

Destinatarios:
{recipients}

Intereses:
{interests}
"""


def _legend(vocab: dict[str, str]) -> str:
    return "\n".join(f"  {key} = {label}" for key, label in vocab.items())


RESPONSE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["queries", "summary"],
    "properties": {
        "queries": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "occasions", "recipients", "interests"],
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "La consulta en español, en minúsculas.",
                    },
                    "occasions": {
                        "type": "array",
                        "items": {"type": "string", "enum": vocabularies.OCCASION_KEYS},
                    },
                    "recipients": {
                        "type": "array",
                        "items": {"type": "string", "enum": vocabularies.RECIPIENT_KEYS},
                    },
                    "interests": {
                        "type": "array",
                        "items": {"type": "string", "enum": vocabularies.INTEREST_KEYS},
                    },
                },
            },
        },
        "summary": {
            "type": "string",
            "description": "Una frase sobre por qué funciona como regalo.",
        },
    },
}


@dataclass(slots=True)
class FacetSpec:
    """One facet the LLM proposed, validated and ready to become a `ProductFacet` row."""

    text: str
    facet_type: str
    weight: float = 1.0
    occasions: list[str] = field(default_factory=list)
    recipients: list[str] = field(default_factory=list)
    interests: list[str] = field(default_factory=list)


def build_system_prompt(*, min_queries: int, max_queries: int) -> str:
    """The enum in the schema constrains the model to valid keys, but a key is a slug: nothing in
    `nino` says "6-11 años". Without this legend the model tags by guessing what the slug means.
    """
    return SYSTEM_PROMPT.format(
        min_q=min_queries,
        max_q=max_queries,
        min_w=MIN_QUERY_WORDS,
        max_w=MAX_QUERY_WORDS,
        occasions=_legend(vocabularies.OCCASIONS),
        recipients=_legend(vocabularies.RECIPIENTS),
        interests=_legend(vocabularies.INTERESTS),
    )


def build_user_prompt(product) -> str:
    """Everything the model needs about one product, and nothing that would mislead it."""
    lines = [f"Título: {product.title}"]
    if product.brand:
        lines.append(f"Marca: {product.brand}")

    category = categories.name_for(product.root_category_id)
    if category:
        lines.append(f"Categoría: {category}")
    if product.product_group:
        lines.append(f"Tipo: {product.product_group}")

    # Price band, never the price. The model should know a €200 gift reads differently from a €20
    # one, but it must never be able to write a number into a query.
    if product.price_band:
        lines.append(f"Rango de precio: {product.get_price_band_display()}")

    features = [
        f.strip()[:MAX_FEATURE_CHARS] for f in (product.features or [])[:MAX_FEATURES] if f.strip()
    ]
    if features:
        lines.append("Características:")
        lines.extend(f"- {f}" for f in features)

    description = _collapse(product.description_text or "")[:MAX_DESCRIPTION_CHARS]
    if description:
        lines.append(f"Descripción: {description}")

    return "\n".join(lines)


def parse_response(
    parsed: dict,
    *,
    min_queries: int = DEFAULT_MIN_QUERIES,
    max_queries: int = DEFAULT_MAX_QUERIES,
) -> list[FacetSpec]:
    """Validate the model's output into facet specs.

    Returns an empty list when the model produced too few usable queries — the caller treats that
    as a failure rather than writing a half-enriched product that would rank badly forever.
    """
    from apps.catalog.models import FacetType

    specs: list[FacetSpec] = []
    seen: set[str] = set()

    for item in parsed.get("queries") or []:
        text = _collapse(str(item.get("text") or "")).lower().strip(" .")
        if not _is_usable_query(text) or text in seen:
            continue
        seen.add(text)
        specs.append(
            FacetSpec(
                text=text,
                facet_type=FacetType.SYNTHETIC_QUERY,
                occasions=vocabularies.clean(
                    item.get("occasions"), vocabularies.OCCASIONS, limit=MAX_OCCASIONS_PER_FACET
                ),
                recipients=vocabularies.clean(
                    item.get("recipients"), vocabularies.RECIPIENTS, limit=MAX_RECIPIENTS_PER_FACET
                ),
                interests=vocabularies.clean(
                    item.get("interests"), vocabularies.INTERESTS, limit=MAX_INTERESTS_PER_FACET
                ),
            )
        )
        if len(specs) >= max_queries:
            break

    if len(specs) < min_queries:
        return []

    summary = _collapse(str(parsed.get("summary") or ""))
    if summary:
        specs.append(
            FacetSpec(
                text=summary,
                facet_type=FacetType.SUMMARY,
                weight=SUMMARY_WEIGHT,
                # The summary describes the whole product, so it carries the union of the tags the
                # individual queries earned. That makes it a valid target for slot filtering too.
                occasions=_union(specs, "occasions", MAX_OCCASIONS_PER_FACET * 2),
                recipients=_union(specs, "recipients", MAX_RECIPIENTS_PER_FACET * 2),
                interests=_union(specs, "interests", MAX_INTERESTS_PER_FACET * 2),
            )
        )
    return specs


# --- helpers -----------------------------------------------------------------


_WHITESPACE = re.compile(r"\s+")
_HAS_DIGITS_MONEY = re.compile(r"\d+\s*(?:€|eur|euros)|€", re.IGNORECASE)


def _collapse(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _is_usable_query(text: str) -> bool:
    if not text:
        return False
    if _HAS_DIGITS_MONEY.search(text):
        return False
    return MIN_QUERY_WORDS <= len(text.split()) <= MAX_QUERY_WORDS


def _union(specs: list[FacetSpec], attr: str, limit: int) -> list[str]:
    out: list[str] = []
    for spec in specs:
        for key in getattr(spec, attr):
            if key not in out:
                out.append(key)
            if len(out) >= limit:
                return out
    return out
