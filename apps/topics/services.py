"""Topic services: canonical text, embeddings, curation and the §9.2 quality gate.

Curation deliberately owns no ranking logic of its own. It calls the Phase 6 retrieval stack, so
a topic page and a live search for the same intent can never disagree about what a good product
is. If ranking improves, every topic improves with it on the next `curate_topics` run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from pgvector.django import CosineDistance

from apps.catalog import vocabularies
from apps.search import engine
from apps.topics.models import LinkSource, ProductTopicLink, Topic, TopicAlias, TopicStatus

logger = logging.getLogger(__name__)

#: How many products to try to link per topic. Deeper than a page, because the quality gate wants
#: ≥12 survivors and pinning/exclusion eats into the list.
LINKS_PER_TOPIC = 36

#: A link below this score is not worth a slot on a landing page; §9.2 counts only links above it.
MIN_LINK_SCORE = 0.55

#: Re-curate a topic at least this often, so new products enter existing topics on their own.
RECURATE_AFTER_DAYS = 14


@dataclass(frozen=True)
class TopicQualityGate:
    """§9.2. A failing topic stays live for users but goes `noindex` and leaves the sitemap."""

    min_links: int = 12
    min_score: float = MIN_LINK_SCORE
    min_categories: int = 4
    min_brands: int = 3
    require_intro: bool = True
    require_human_review: bool = True

    def evaluate(self, topic: Topic) -> str:
        """Return a Spanish reason the topic is not indexable, or "" if it passes."""
        if topic.merged_into_id:
            return "Fusionado con otro tema"
        if topic.linked_count < self.min_links:
            return f"Solo {topic.linked_count} productos (mínimo {self.min_links})"
        if topic.distinct_categories < self.min_categories:
            return f"Solo {topic.distinct_categories} categorías (mínimo {self.min_categories})"
        if topic.distinct_brands < self.min_brands:
            return f"Solo {topic.distinct_brands} marcas (mínimo {self.min_brands})"
        if self.require_intro and not topic.intro_html.strip():
            return "Falta la introducción"
        if self.require_human_review and not topic.human_reviewed:
            return "Pendiente de revisión humana"
        return ""


def build_canonical_text(topic: Topic) -> str:
    """The sentence that represents the topic in embedding space.

    Built the same way `normalize.slots_to_sentence` builds an advanced-search query, so a topic
    and the search that should match it land in the same neighbourhood.

    Facets already named in the title are skipped. A well-written title usually states the
    recipient or occasion outright, and appending it again produced "Regalos para madres para
    madre" — repetition that shifts the vector without adding meaning. Nothing is lost by
    dropping it: the facets still apply as hard filters via `topic_slots`.
    """
    from apps.search.normalize import normalize

    title = topic.title.strip()
    normalized_title = normalize(title)
    parts = [title]

    for field_name, template in (
        ("recipients", "para {}"),
        ("occasions", "en {}"),
        ("interests", "a quien le gusta {}"),
    ):
        labels = [
            label
            for key in (getattr(topic, field_name) or [])
            if (label := _clean_label(key)) and not _already_in(label, normalized_title)
        ]
        if labels:
            parts.append(template.format(_join(labels)))

    return " ".join(parts)


def _clean_label(key: str) -> str:
    """Drop the parenthetical gloss the vocabulary carries for the LLM, e.g. "(3-5 años)"."""
    return vocabularies.label(key).split("(")[0].strip().lower()


def _already_in(label: str, normalized_title: str) -> bool:
    """Prefix match on the first word, so "madre" matches "madres" and "cocina" matches "cocinar"."""
    from apps.search.normalize import normalize

    head = normalize(label).split()
    if not head:
        return False
    return head[0][:5] in normalized_title


def _join(values: list[str]) -> str:
    if len(values) == 1:
        return values[0]
    return f"{', '.join(values[:-1])} y {values[-1]}"


def topic_slots(topic: Topic) -> dict:
    """Topic facets become hard search filters, exactly as advanced-search slots do."""
    slots: dict = {}
    if topic.occasions:
        slots["occasions"] = list(topic.occasions)
    if topic.recipients:
        slots["recipients"] = list(topic.recipients)
    if topic.interests:
        slots["interests"] = list(topic.interests)
    if topic.price_band:
        slots["price_bands"] = [topic.price_band]
    return slots


def needs_curation(topic: Topic, *, now=None) -> bool:
    now = now or timezone.now()
    if topic.last_curated_at is None:
        return True
    return (now - topic.last_curated_at).days >= RECURATE_AFTER_DAYS


@transaction.atomic
def curate(topic: Topic, *, gate: TopicQualityGate | None = None, llm=None) -> dict:
    """Rewrite a topic's product links from live retrieval, then re-run the quality gate.

    Pinned and excluded links survive, because they represent a human decision and the whole
    point of a recompute is to update the machine's opinion, not to overrule the editor's.
    """
    gate = gate or TopicQualityGate()

    pinned = list(topic.links.filter(is_pinned=True).select_related("product"))
    excluded_ids = set(topic.links.filter(is_excluded=True).values_list("product_id", flat=True))
    pinned_ids = {link.product_id for link in pinned}

    slots = topic_slots(topic)
    query_text = topic.canonical_text or build_canonical_text(topic)
    response = engine.search(
        query_text,
        slots=slots,
        limit=LINKS_PER_TOPIC,
        use_cache=False,  # curation must see current data, not a cached ordering
        llm=llm,
    )

    # Rewrite only the auto rows; the human's rows are untouched.
    topic.links.filter(is_pinned=False, is_excluded=False).delete()

    new_links: list[ProductTopicLink] = []
    rank = len(pinned)
    for result in response.results:
        product_id = result.product.pk
        if product_id in excluded_ids or product_id in pinned_ids:
            continue
        if result.score < gate.min_score:
            continue
        rank += 1
        new_links.append(
            ProductTopicLink(
                topic=topic,
                product=result.product,
                score=result.score,
                rank=rank,
                source=LinkSource.AUTO,
            )
        )
    ProductTopicLink.objects.bulk_create(new_links, ignore_conflicts=True)

    # Pinned links always occupy the top slots, in their existing order.
    for position, link in enumerate(pinned, start=1):
        if link.rank != position:
            link.rank = position
            link.save(update_fields=["rank"])

    return _apply_gate(topic, gate)


def _apply_gate(topic: Topic, gate: TopicQualityGate) -> dict:
    """Recompute the §9.2 counters, then set `is_indexable`."""
    rows = topic.links.filter(is_excluded=False, score__gte=gate.min_score).select_related(
        "product"
    )
    products = [link.product for link in rows]

    was_indexable = topic.is_indexable
    topic.linked_count = len(products)
    topic.distinct_categories = len({p.root_category_id for p in products if p.root_category_id})
    topic.distinct_brands = len({(p.brand or "").lower() for p in products if p.brand})
    topic.quality_score = sum(link.score for link in rows) / len(products) if products else 0.0

    reason = gate.evaluate(topic)
    topic.is_indexable = not reason
    topic.last_curated_at = timezone.now()
    topic.save(
        update_fields=[
            "linked_count",
            "distinct_categories",
            "distinct_brands",
            "quality_score",
            "is_indexable",
            "last_curated_at",
            "updated_at",
        ]
    )

    return {
        "linked": topic.linked_count,
        "categories": topic.distinct_categories,
        "brands": topic.distinct_brands,
        "is_indexable": topic.is_indexable,
        "lost_indexable": was_indexable and not topic.is_indexable,
        "reason": reason,
    }


def embed_topic(topic: Topic, *, llm) -> None:
    """Set `canonical_text` and its embedding. Needed before a topic can match a user query."""
    topic.canonical_text = build_canonical_text(topic)
    topic.embedding = llm.embed_one(topic.canonical_text, purpose="topic_embedding")
    topic.save(update_fields=["canonical_text", "embedding", "updated_at"])


# --- read side: everything the public site needs ---------------------------------------------

#: How close a user's query must be to a topic before we redirect them to it instead of showing
#: search results. Deliberately strict: sending someone to the wrong landing page is much worse
#: than showing them a result list, because the page then lies about what they asked for.
TOPIC_REDIRECT_SIMILARITY = 0.90


def visible_links(topic: Topic):
    """The products a topic page actually shows, in rank order.

    Excluded links stay in the table — an editor's "no" is a decision worth keeping — but they
    never reach a page, and `_apply_gate` does not count them either.
    """
    return (
        ProductTopicLink.objects.filter(topic=topic, is_excluded=False)
        .select_related("product")
        .filter(product__is_active=True, product__is_blocked=False)
        .order_by("rank", "-score")
    )


def related_topics(topic: Topic, *, limit: int = 6) -> list[Topic]:
    """Siblings for the "regalos relacionados" block (§8.4 internal linking).

    Vector-nearest where possible, because that is the same notion of similarity the rest of the
    site uses. Without an embedding it falls back to same-kind topics, which is weaker but never
    leaves the block empty — an empty related block is a dead end for both users and crawlers.
    """
    base = Topic.objects.filter(
        status=TopicStatus.ACTIVE, is_indexable=True, merged_into__isnull=True
    ).exclude(pk=topic.pk)

    if topic.embedding is None:
        return list(base.filter(kind=topic.kind).order_by("-priority")[:limit])

    return list(
        base.exclude(embedding__isnull=True)
        .annotate(distance=CosineDistance("embedding", topic.embedding))
        .order_by("distance")[:limit]
    )


def seasonal_topics(*, limit: int = 4, today=None) -> list[Topic]:
    """Topics whose season is open right now. Ranges may wrap the year end (Navidad)."""
    today = today or timezone.localdate()
    open_now = Q(seasonal_start__lte=today, seasonal_end__gte=today)
    wrapping = Q(seasonal_start__gt=F("seasonal_end")) & (
        Q(seasonal_start__lte=today) | Q(seasonal_end__gte=today)
    )
    return list(
        Topic.objects.filter(
            status=TopicStatus.ACTIVE,
            is_indexable=True,
            merged_into__isnull=True,
            seasonal_start__isnull=False,
            seasonal_end__isnull=False,
        )
        .filter(open_now | wrapping)
        .order_by("-priority")[:limit]
    )


def suggestions(*, limit: int = 6) -> list[Topic]:
    """What to offer when a search finds nothing. Never an empty page."""
    return list(
        Topic.objects.filter(
            status=TopicStatus.ACTIVE, is_indexable=True, merged_into__isnull=True
        ).order_by("-priority", "-quality_score")[:limit]
    )


def topic_for_alias_slug(slug: str) -> Topic | None:
    """Resolve a dead slug through its alias, following one merge hop."""
    alias = (
        TopicAlias.objects.filter(normalized=slug.replace("-", " "))
        .select_related("topic", "topic__merged_into")
        .first()
    )
    if alias is None:
        return None
    return alias.topic.merged_into or alias.topic


def match_topic(normalized: str, *, llm=None) -> Topic | None:
    """Find the curated topic a query should be redirected to, or None.

    An exact alias hit wins outright: an alias is a phrasing someone deliberately recorded as
    meaning this topic. Otherwise we fall back to the embedding, and only above a strict
    threshold. The alias `hits` counter is what later shows which phrasings real users type.
    """
    if not normalized:
        return None

    alias = (
        TopicAlias.objects.filter(normalized=normalized)
        .select_related("topic", "topic__merged_into")
        .first()
    )
    if alias is not None:
        TopicAlias.objects.filter(pk=alias.pk).update(hits=F("hits") + 1)
        candidate = alias.topic.merged_into or alias.topic
        return candidate if _is_servable(candidate) else None

    client = llm or engine._shared_client()
    try:
        embedding = client.embed_one(normalized, purpose="topic_match")
    except Exception:  # noqa: BLE001 - a redirect is a nicety; search still works without it
        logger.warning("No se pudo incrustar %r para emparejar tema", normalized, exc_info=True)
        return None

    best = (
        Topic.objects.filter(status=TopicStatus.ACTIVE, is_indexable=True, merged_into__isnull=True)
        .exclude(embedding__isnull=True)
        .annotate(distance=CosineDistance("embedding", embedding))
        .order_by("distance")
        .first()
    )
    if best is None or (1 - float(best.distance)) < TOPIC_REDIRECT_SIMILARITY:
        return None
    return best


def _is_servable(topic: Topic) -> bool:
    return topic.status == TopicStatus.ACTIVE and topic.merged_into_id is None
