"""Topic pipelines: curation, decomposition and deduplication.

`curate_topics` is the important one. It calls the Phase 6 retrieval stack rather than
reimplementing ranking, so topic pages and live search always agree. The others keep the topic
set healthy: `decompose_topic` turns a topic into Amazon keywords for targeted ingestion, and
`dedupe_topics` flags near-identical topics before they compete with each other in search.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from pgvector.django import CosineDistance

from apps.clients.exceptions import LLMRequestFailed
from apps.clients.telegram import TelegramClient
from apps.pipelines.base import Pipeline, PipelineContext, PipelineResult
from apps.pipelines.models import NotificationLevel
from apps.pipelines.registry import register
from apps.topics import services
from apps.topics.models import (
    AliasSource,
    SearchTerm,
    SearchTermStatus,
    Topic,
    TopicAlias,
    TopicStatus,
)

logger = logging.getLogger(__name__)


@register
class CurateTopicsPipeline(Pipeline):
    """Rewrite `ProductTopicLink` for stale topics and re-apply the §9.2 quality gate.

    This is what makes the site self-healing (§6.6): products enriched today enter existing topics
    tomorrow without anyone touching the topic, and topics whose products went inactive quietly
    lose `is_indexable` instead of shipping a thin page to Google.
    """

    key = "curate_topics"
    description = "Recalcula los productos de cada tema y aplica la puerta de calidad."
    default_cron = "0 5 * * *"
    default_max_per_run = 25
    priority = 40
    requires_llm = True  # one embedding per topic, no chat completion
    default_options = {
        # Curate every topic regardless of `last_curated_at`. Use after changing ranking weights.
        "force": False,
        # Topics below the gate stay live but noindex; a run that silently drops a page out of
        # the sitemap should be visible, so it is reported rather than just logged.
        "include_drafts": True,
    }

    def select(self, ctx: PipelineContext):
        statuses = [TopicStatus.ACTIVE]
        if ctx.option("include_drafts", True):
            statuses.append(TopicStatus.DRAFT)

        qs = Topic.objects.filter(status__in=statuses, merged_into__isnull=True)
        if not ctx.option("force", False):
            cutoff = timezone.now() - timedelta(days=services.RECURATE_AFTER_DAYS)
            qs = qs.filter(Q(last_curated_at__isnull=True) | Q(last_curated_at__lt=cutoff))

        # Never-curated topics first, then the stalest, then by editorial priority.
        return qs.order_by("last_curated_at", "-priority", "id")

    def run(self, ctx: PipelineContext) -> PipelineResult:
        topics = list(self.select(ctx)[: ctx.max_items])
        result = PipelineResult(items_in=len(topics))
        if not topics:
            return result

        lost_indexable: list[str] = []
        indexable = 0

        with ctx.step("curate", topics=len(topics)) as step:
            for topic in topics:
                try:
                    if topic.embedding is None or not topic.canonical_text:
                        services.embed_topic(topic, llm=ctx.llm)
                    outcome = services.curate(topic, llm=ctx.llm)
                except LLMRequestFailed as exc:
                    result.items_failed += 1
                    logger.warning("No se pudo curar el tema %s: %s", topic.slug, exc)
                    continue

                result.items_updated += 1
                if outcome["is_indexable"]:
                    indexable += 1
                if outcome["lost_indexable"]:
                    lost_indexable.append(f"{topic.slug} ({outcome['reason']})")

            step.context.update(indexable=indexable, lost_indexable=len(lost_indexable))

        # §6.6 requires a warning when a live page falls out of the index, because that is a
        # traffic loss that would otherwise only surface in Search Console weeks later.
        if lost_indexable:
            TelegramClient().notify(
                "topics.lost_indexable",
                "Temas que han dejado de ser indexables:\n" + "\n".join(lost_indexable[:20]),
                level=NotificationLevel.WARNING,
            )

        result.context.update(indexable=indexable, lost_indexable=lost_indexable[:20])
        return result


DECOMPOSE_SYSTEM = """Eres un experto en el catálogo de Amazon España.
Recibes un tema de regalos y devuelves términos de búsqueda que usarías en el buscador de Amazon
para encontrar productos que encajen en ese tema.

Reglas:
- Entre {min_terms} y {max_terms} términos.
- Cada término: de 2 a 5 palabras, en español, tal y como se escribiría en Amazon.
- Son términos de CATÁLOGO, no frases de regalo: "set jardinería principiantes", no
  "regalo para mi madre".
- Cubre tipos de producto distintos entre sí. No repitas sinónimos del mismo objeto.
- No incluyas marcas, precios, ni las palabras "regalo" o "barato".
"""

DECOMPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "terms": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["terms"],
    "additionalProperties": False,
}


@register
class DecomposeTopicPipeline(Pipeline):
    """Turn a Topic into Amazon `SearchTerm`s, so ingestion can be aimed at content gaps.

    Seeding by category alone fills the catalogue with whatever Amazon ranks highly; seeding by
    topic fills it with what the site actually needs in order to publish pages.
    """

    key = "decompose_topic"
    description = "Convierte temas en términos de búsqueda de Amazon."
    default_cron = "0 4 * * *"
    default_max_per_run = 10
    priority = 50
    requires_llm = True
    default_options = {"min_terms": 5, "max_terms": 10, "redecompose": False}

    def select(self, ctx: PipelineContext):
        qs = Topic.objects.filter(merged_into__isnull=True).exclude(status=TopicStatus.ARCHIVED)
        if not ctx.option("redecompose", False):
            qs = qs.filter(search_terms__isnull=True)
        return qs.order_by("-priority", "id").distinct()

    def run(self, ctx: PipelineContext) -> PipelineResult:
        topics = list(self.select(ctx)[: ctx.max_items])
        result = PipelineResult(items_in=len(topics))
        if not topics:
            return result

        min_terms = int(ctx.option("min_terms", 5))
        max_terms = int(ctx.option("max_terms", 10))
        system = DECOMPOSE_SYSTEM.format(min_terms=min_terms, max_terms=max_terms)

        with ctx.step("decompose", topics=len(topics)) as step:
            created = 0
            for topic in topics:
                try:
                    response = ctx.llm.complete(
                        system=system,
                        user=self._user_prompt(topic),
                        json_schema=DECOMPOSE_SCHEMA,
                        schema_name="search_terms",
                        purpose="decompose_topic",
                    )
                except LLMRequestFailed as exc:
                    result.items_failed += 1
                    logger.warning("No se pudo descomponer %s: %s", topic.slug, exc)
                    continue

                terms = self._parse(response.parsed, max_terms)
                if not terms:
                    result.items_failed += 1
                    continue

                for text in terms:
                    _, was_created = SearchTerm.objects.get_or_create(
                        topic=topic,
                        normalized=text,
                        defaults={"text": text, "status": SearchTermStatus.PENDING},
                    )
                    created += int(was_created)
                result.items_updated += 1

            step.context.update(terms_created=created)
            result.items_created = created

        return result

    def _user_prompt(self, topic: Topic) -> str:
        lines = [f"Tema: {topic.title}"]
        if topic.recipients:
            lines.append(f"Destinatario: {', '.join(topic.recipients)}")
        if topic.occasions:
            lines.append(f"Ocasión: {', '.join(topic.occasions)}")
        if topic.interests:
            lines.append(f"Intereses: {', '.join(topic.interests)}")
        if topic.price_band:
            lines.append(f"Presupuesto: {topic.price_band}")
        return "\n".join(lines)

    def _parse(self, parsed, max_terms: int) -> list[str]:
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except json.JSONDecodeError:
                return []
        raw = (parsed or {}).get("terms") or []

        seen: set[str] = set()
        out: list[str] = []
        for item in raw:
            text = " ".join(str(item).lower().split()).strip(" .")
            if not (2 <= len(text.split()) <= 6) or text in seen:
                continue
            seen.add(text)
            out.append(text)
        return out[:max_terms]


@register
class DedupeTopicsPipeline(Pipeline):
    """Flag near-identical topics so they stop competing with each other.

    Two pages about the same intent split their own backlinks and confuse Google about which to
    rank. Merging is never automatic (§6.6): the pipeline records the pair and an editor confirms
    it in Admin, because a wrong merge cascades into redirects that are painful to unwind.
    """

    key = "dedupe_topics"
    description = "Detecta temas casi duplicados y los propone para fusión."
    default_cron = "0 6 * * 1"
    default_max_per_run = 200
    priority = 70
    default_options = {"threshold": 0.95}

    def select(self, ctx: PipelineContext):
        return Topic.objects.filter(embedding__isnull=False, merged_into__isnull=True).order_by(
            "-priority", "id"
        )

    def run(self, ctx: PipelineContext) -> PipelineResult:
        threshold = float(ctx.option("threshold", 0.95))
        max_distance = 1.0 - threshold
        topics = list(self.select(ctx)[: ctx.max_items])
        result = PipelineResult(items_in=len(topics))
        if not topics:
            return result

        pairs: list[str] = []
        with ctx.step("compare", topics=len(topics), threshold=threshold) as step:
            for topic in topics:
                match = (
                    Topic.objects.filter(embedding__isnull=False, merged_into__isnull=True)
                    .exclude(pk=topic.pk)
                    .annotate(distance=CosineDistance("embedding", topic.embedding))
                    .filter(distance__lte=max_distance)
                    .order_by("distance")
                    .first()
                )
                if match is None:
                    continue

                # Keep the higher-priority topic; ties break on age, so the original survives.
                keeper, loser = sorted((topic, match), key=lambda t: (-t.priority, t.created_at))
                # Never propose demoting an indexed page in favour of one that is not.
                if loser.is_indexable and not keeper.is_indexable:
                    continue

                _, created = TopicAlias.objects.get_or_create(
                    normalized=loser.canonical_text[:255],
                    defaults={
                        "topic": keeper,
                        "text": loser.title,
                        "source": AliasSource.AUTO,
                    },
                )
                if created:
                    result.items_created += 1
                    pairs.append(f"{loser.slug} → {keeper.slug}")

            step.context.update(pairs=len(pairs))

        if pairs:
            TelegramClient().notify(
                "topics.duplicates",
                "Temas casi duplicados pendientes de confirmar:\n" + "\n".join(pairs[:20]),
                level=NotificationLevel.WARNING,
            )
        result.context.update(pairs=pairs[:20])
        return result
