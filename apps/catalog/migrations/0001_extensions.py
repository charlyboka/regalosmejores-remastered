"""Postgres extensions the whole project depends on.

They already exist on the Heroku database; ``CREATE EXTENSION IF NOT EXISTS`` makes this a
no-op there, and reproducible anywhere else.
"""

from django.contrib.postgres.operations import (
    BtreeGinExtension,
    TrigramExtension,
    UnaccentExtension,
)
from django.db import migrations
from pgvector.django import VectorExtension


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        VectorExtension(),
        TrigramExtension(),
        UnaccentExtension(),
        BtreeGinExtension(),
    ]
