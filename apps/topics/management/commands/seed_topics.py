"""Seed a small set of hand-written topics.

Deliberately small and hand-written. These are the highest-traffic gift intents in Spain, and
they double as the reference set for judging whether `generate_topics` is producing anything
worth publishing later. Every one is idempotent on `slug`, so re-running is safe.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.clients.llm import LLMClient
from apps.topics import services
from apps.topics.models import Topic, TopicKind, TopicSource, TopicStatus

#: (slug, title, kind, occasions, recipients, interests, price_band, priority)
SEED_TOPICS: list[tuple] = [
    (
        "regalos-para-madres",
        "Regalos para madres",
        TopicKind.RECIPIENT,
        [],
        ["madre"],
        [],
        None,
        90,
    ),
    (
        "regalos-para-padres",
        "Regalos para padres",
        TopicKind.RECIPIENT,
        [],
        ["padre"],
        [],
        None,
        90,
    ),
    (
        "regalos-de-navidad",
        "Regalos de Navidad",
        TopicKind.OCCASION,
        ["navidad"],
        [],
        [],
        None,
        95,
    ),
    (
        "amigo-invisible-barato",
        "Regalos para el amigo invisible",
        TopicKind.HYBRID,
        ["amigo-invisible"],
        ["companero-trabajo"],
        [],
        "ECONOMICO",
        85,
    ),
    (
        "regalos-para-ninos-de-5-anos",
        "Regalos para niños de 5 años",
        TopicKind.RECIPIENT,
        [],
        ["nino-pequeno"],
        [],
        None,
        80,
    ),
    (
        "regalos-para-adolescentes",
        "Regalos para adolescentes",
        TopicKind.RECIPIENT,
        [],
        ["adolescente"],
        [],
        None,
        80,
    ),
    (
        "regalos-para-parejas",
        "Regalos para tu pareja",
        TopicKind.RECIPIENT,
        [],
        ["pareja"],
        [],
        None,
        85,
    ),
    (
        "regalos-de-cumpleanos",
        "Regalos de cumpleaños",
        TopicKind.OCCASION,
        ["cumpleanos"],
        [],
        [],
        None,
        95,
    ),
    (
        "regalos-para-abuelos",
        "Regalos para abuelos",
        TopicKind.RECIPIENT,
        [],
        ["abuela", "abuelo"],
        [],
        None,
        70,
    ),
    (
        "regalos-para-manualidades",
        "Regalos para quien disfruta de las manualidades",
        TopicKind.INTEREST,
        [],
        [],
        ["manualidades"],
        None,
        65,
    ),
    (
        "regalos-tecnologicos",
        "Regalos tecnológicos",
        TopicKind.INTEREST,
        [],
        [],
        ["tecnologia"],
        None,
        75,
    ),
    (
        "regalos-para-cocinillas",
        "Regalos para quien ama cocinar",
        TopicKind.INTEREST,
        [],
        [],
        ["cocina"],
        None,
        70,
    ),
]


class Command(BaseCommand):
    help = "Crea los temas iniciales escritos a mano."

    def add_arguments(self, parser):
        parser.add_argument(
            "--embed",
            action="store_true",
            help="Calcula también el embedding de cada tema (una llamada por tema).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        llm = LLMClient() if options["embed"] else None
        created = updated = 0

        for slug, title, kind, occasions, recipients, interests, band, priority in SEED_TOPICS:
            topic, was_created = Topic.objects.update_or_create(
                slug=slug,
                defaults={
                    "title": title,
                    "kind": kind,
                    "occasions": occasions,
                    "recipients": recipients,
                    "interests": interests,
                    "price_band": band,
                    "priority": priority,
                    "source": TopicSource.MANUAL,
                    # DRAFT on purpose: §9.3 caps publication at 10/day and nothing goes live
                    # before a human has read the intro.
                    "status": TopicStatus.DRAFT,
                },
            )
            created += int(was_created)
            updated += int(not was_created)

            topic.canonical_text = services.build_canonical_text(topic)
            topic.save(update_fields=["canonical_text"])

            if llm is not None:
                services.embed_topic(topic, llm=llm)

            self.stdout.write(
                f"  {'+' if was_created else '=':>2} {slug}  ({topic.canonical_text})"
            )

        self.stdout.write(self.style.SUCCESS(f"\n{created} temas creados, {updated} actualizados."))
        if llm is None:
            self.stdout.write("Sin embeddings. Vuelve a ejecutar con --embed cuando quieras.")
