"""Unaccented Spanish text-search configuration.

Queries are normalised to ASCII before they reach the database ("niño" -> "nino"), but the FTS
index was built with the stock `spanish` configuration, which keeps accents. The result was that
the lexical arm of hybrid retrieval matched nothing for any query containing an accented word —
which in Spanish is most of them. Only the trigram arm was firing, by accident, because "nino"
and "niño" happen to share enough trigrams.

This creates a `spanish_unaccent` configuration that runs `unaccent` before the Spanish stemmer,
and rebuilds the facet index against it. Both sides of the comparison are now accent-free.
"""

from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVector
from django.db import migrations

CREATE_CONFIG = """
DROP TEXT SEARCH CONFIGURATION IF EXISTS spanish_unaccent;
CREATE TEXT SEARCH CONFIGURATION spanish_unaccent (COPY = spanish);
ALTER TEXT SEARCH CONFIGURATION spanish_unaccent
    ALTER MAPPING FOR hword, hword_part, word
    WITH unaccent, spanish_stem;
"""

DROP_CONFIG = "DROP TEXT SEARCH CONFIGURATION IF EXISTS spanish_unaccent;"


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0004_product_binding_alter_product_product_group"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_CONFIG, reverse_sql=DROP_CONFIG),
        migrations.RemoveIndex(model_name="productfacet", name="facet_text_fts_idx"),
        migrations.AddIndex(
            model_name="productfacet",
            index=GinIndex(
                SearchVector("text", config="spanish_unaccent"), name="facet_text_fts_idx"
            ),
        ),
    ]
