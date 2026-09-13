"""Seed the RankingConfig singleton so Admin always has a row to edit."""

from django.db import migrations


def create_singleton(apps, schema_editor):
    RankingConfig = apps.get_model("search", "RankingConfig")
    RankingConfig.objects.get_or_create(pk=1)


def drop_singleton(apps, schema_editor):
    RankingConfig = apps.get_model("search", "RankingConfig")
    RankingConfig.objects.filter(pk=1).delete()


class Migration(migrations.Migration):
    dependencies = [("search", "0001_initial")]

    operations = [migrations.RunPython(create_singleton, drop_singleton)]
