"""amazon.es root categories.

Fetched live from Keepa's ``/category`` endpoint (domain 9) and frozen here because the tree
changes slowly and we do not want to spend a token on it every run. Refresh with
``manage.py sync_categories`` if the counts ever look wrong.

``product_count`` is not decoration: it is the denominator for ``sales_rank_pct``. Sales rank
1 000 means something very different in Videojuegos (457 k products) than in Hogar y cocina
(51 M), and the quality score has to reflect that.

``gift_suitable`` is the seeding allowlist. Everything outside it is still recorded when we
encounter it — we just never harvest from it and never enrich it.
"""

from __future__ import annotations

from typing import NamedTuple


class RootCategory(NamedTuple):
    cat_id: int
    name: str
    product_count: int
    gift_suitable: bool


ROOT_CATEGORIES: tuple[RootCategory, ...] = (
    # --- in scope ----------------------------------------------------------------------
    RootCategory(599391031, "Hogar y cocina", 51_397_855, True),
    RootCategory(1951051031, "Coche y moto", 26_119_533, True),
    RootCategory(2454133031, "Bricolaje y herramientas", 19_153_834, True),
    RootCategory(599370031, "Electrónica", 15_133_568, True),
    RootCategory(1571259031, "Jardín", 12_271_555, True),
    RootCategory(2454136031, "Deportes y aire libre", 11_588_734, True),
    RootCategory(6198054031, "Belleza", 4_956_911, True),
    RootCategory(667049031, "Informática", 4_926_301, True),
    RootCategory(599385031, "Juguetes y juegos", 4_883_206, True),
    RootCategory(12472654031, "Productos para mascotas", 4_882_080, True),
    RootCategory(3628728031, "Oficina y papelería", 4_433_401, True),
    RootCategory(3628866031, "Instrumentos musicales", 2_550_447, True),
    RootCategory(3564289031, "Iluminación", 2_528_589, True),
    RootCategory(1703495031, "Bebé", 2_301_905, True),
    RootCategory(599382031, "Videojuegos", 456_526, True),
    # --- out of scope, kept for the rank denominator and for explicit exclusion ---------
    RootCategory(599364031, "Libros", 65_598_210, False),
    RootCategory(5512276031, "Moda", 51_921_624, False),
    RootCategory(5866088031, "Industria, empresas y ciencia", 11_730_985, False),
    RootCategory(1748200031, "Música digital", 6_733_102, False),
    RootCategory(3677430031, "Salud y cuidado personal", 4_174_627, False),
    RootCategory(599373031, "CD y vinilos", 1_854_963, False),
    RootCategory(4772050031, "Grandes electrodomésticos", 763_564, False),
    RootCategory(599379031, "Películas y TV", 757_217, False),
    RootCategory(6198072031, "Alimentación y bebidas", 513_318, False),
    RootCategory(667040031, "Otros Productos", 444_987, False),
    RootCategory(818936031, "Tienda Kindle", 175_063, False),
    RootCategory(1661649031, "Aplicaciones y juegos", 86_483, False),
    RootCategory(16296206031, "Prime Video", 57_061, False),
    RootCategory(599376031, "Software", 10_287, False),
    RootCategory(22942672031, "Amazon Luxury", 1_762, False),
    RootCategory(13944662031, "Alexa Skills", 1_211, False),
    RootCategory(3564279031, "Cheques regalo", 997, False),
    RootCategory(12598806031, "Dispositivos Amazon y accesorios", 571, False),
    RootCategory(9839995031, "Belleza Premium", 38, False),
    RootCategory(17465193031, "Audible Libros y Originales", 17, False),
    RootCategory(9699482031, "Productos Handmade", 0, False),
)

BY_ID: dict[int, RootCategory] = {c.cat_id: c for c in ROOT_CATEGORIES}

GIFT_SUITABLE_IDS: tuple[int, ...] = tuple(c.cat_id for c in ROOT_CATEGORIES if c.gift_suitable)

#: Fallback denominator when a product's root category is unknown to us.
DEFAULT_PRODUCT_COUNT = 1_000_000

#: Keepa ``type`` values that are never gifts, whatever category they are filed under.
#: Stored on ``Product.product_group``. Keepa's old ``productGroup`` field is null on every
#: response now, so ``type`` is the field that actually carries this information.
#: This is a second net under the category allowlist: Blu-rays turn up filed under Electrónica.
BLOCKED_PRODUCT_GROUPS: frozenset[str] = frozenset(
    {
        "physical_movie",
        "digital_movie",
        "digital_video_download",
        "physical_book",
        "digital_ebook",
        "abis_book",
        "physical_music",
        "digital_music",
        "digital_music_album",
        "digital_music_track",
        "digital_periodical",
        "physical_magazine",
        "software",
        "digital_software",
        "mobile_application",
        "video_games_download",
        "digital_video_game",
        "gift_card",
        "grocery",
        "subscription",
        "digital_subscription",
        # Consumables. Nobody gives these as a present, and they dominate the top sales ranks
        # of Electrónica, so without this the catalogue fills up with AA batteries.
        "battery",
        "batteries",
        "ink_or_toner",
        "printer_ink",
        "lightbulb",
        "cleaning_agent",
        "paper_product",
        # Amazon first-party hardware. These pass every quality filter, but they earn 0% Associates
        # commission in Spain and have no discovery value: nobody needs this site to learn that a
        # Kindle exists. They also dominate the top ranks of Electronica and Informatica.
        "amazon_book_reader",
        "digital_device_3",
        "digital_device_4",
    }
)

#: Keepa ``binding`` values that mean "this is media", used as a backstop when ``type`` is empty.
BLOCKED_BINDINGS: frozenset[str] = frozenset(
    {
        "blu_ray",
        "dvd",
        "audio_cd",
        "vinyl",
        "vinilo",
        "kindle_edition",
        "audible_audiobook",
        "mp3_download",
        "tapa_blanda",
        "tapa_dura",
        "libro_de_bolsillo",
        "pasta_blanda",
        "pasta_dura",
        "paperback",
        "hardcover",
        "mass_market_paperback",
        "app",
        "software_download",
        "video_game_download",
    }
)


def name_for(cat_id: int | None) -> str:
    category = BY_ID.get(cat_id or 0)
    return category.name if category else ""


def product_count_for(cat_id: int | None) -> int:
    category = BY_ID.get(cat_id or 0)
    return category.product_count if category and category.product_count else DEFAULT_PRODUCT_COUNT


def is_gift_suitable(cat_id: int | None) -> bool:
    category = BY_ID.get(cat_id or 0)
    return bool(category and category.gift_suitable)
