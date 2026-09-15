"""Controlled vocabularies for gift tagging.

These three lists are the shared language of the whole site. The enrichment LLM may only tag a
`ProductFacet` with keys from here (enforced by `enum` in the JSON schema, and re-checked in
Python), the advanced search form offers exactly these as chips, and `Topic` rows are tagged with
the same keys. That is what makes slot filtering a plain array-overlap query against a GIN index
instead of fuzzy text matching.

Keys are ASCII slugs so they are safe in URLs and query strings. Labels are the Spanish shown to
users. **Never rename a key** once products are tagged with it — add a new one and re-enrich.
"""

from __future__ import annotations

# --- occasions ---------------------------------------------------------------
# When someone gives a gift. Deliberately short: these map to the Spanish gifting calendar and to
# the topic pages we want to rank for.

OCCASIONS: dict[str, str] = {
    "cumpleanos": "Cumpleaños",
    "navidad": "Navidad",
    "reyes": "Reyes Magos",
    "amigo-invisible": "Amigo invisible",
    "san-valentin": "San Valentín",
    "dia-de-la-madre": "Día de la Madre",
    "dia-del-padre": "Día del Padre",
    "aniversario": "Aniversario",
    "boda": "Boda",
    "graduacion": "Graduación",
    "comunion": "Comunión",
    "bautizo": "Bautizo",
    "baby-shower": "Baby shower",
    "jubilacion": "Jubilación",
    "inauguracion-casa": "Inauguración de casa",
    "nuevo-trabajo": "Nuevo trabajo",
    "agradecimiento": "Agradecimiento",
    "despedida": "Despedida",
    "sin-ocasion": "Sin ocasión especial",
}

# --- recipients --------------------------------------------------------------
# Who receives it. Mixes relationship ("madre") and life stage ("adolescente") on purpose: that is
# how people actually search, and the life-stage keys are what the "Edad" slot in advanced search
# resolves to, since we have no reliable per-product age data from Keepa.

RECIPIENTS: dict[str, str] = {
    # relationship
    "madre": "Madre",
    "padre": "Padre",
    "pareja": "Pareja",
    "esposa": "Esposa",
    "marido": "Marido",
    "hermana": "Hermana",
    "hermano": "Hermano",
    "hija": "Hija",
    "hijo": "Hijo",
    "abuela": "Abuela",
    "abuelo": "Abuelo",
    "amiga": "Amiga",
    "amigo": "Amigo",
    "companero-trabajo": "Compañero de trabajo",
    "jefe": "Jefe o jefa",
    "profesor": "Profesor o profesora",
    "cuidador": "Cuidador o cuidadora",
    "familia": "Toda la familia",
    # life stage
    "bebe": "Bebé (0-2 años)",
    "nino-pequeno": "Niño pequeño (3-5 años)",
    "nino": "Niño o niña (6-11 años)",
    "adolescente": "Adolescente (12-17 años)",
    "adulto-joven": "Adulto joven (18-30 años)",
    "adulto": "Adulto (30-60 años)",
    "senior": "Persona mayor (60+)",
}

# --- interests ---------------------------------------------------------------
# What the person is into. The longest list by design: interest is the strongest gift signal and
# the hardest thing to guess from a product title alone.

INTERESTS: dict[str, str] = {
    # home & food
    "cocina": "Cocina",
    "reposteria": "Repostería",
    "cafe-y-te": "Café y té",
    "vino-y-cocteles": "Vino y cócteles",
    "barbacoa": "Barbacoa",
    "decoracion": "Decoración",
    "organizacion-hogar": "Orden y organización",
    "iluminacion": "Iluminación",
    "jardineria": "Jardinería",
    "plantas": "Plantas de interior",
    # making things
    "bricolaje": "Bricolaje",
    "herramientas": "Herramientas",
    "manualidades": "Manualidades",
    "costura": "Costura y punto",
    "pintura-y-dibujo": "Pintura y dibujo",
    "impresion-3d": "Impresión 3D",
    "modelismo": "Modelismo",
    # play
    "juegos-de-mesa": "Juegos de mesa",
    "puzzles": "Puzzles",
    "construccion": "Juegos de construcción",
    "videojuegos": "Videojuegos",
    "juguetes-educativos": "Juguetes educativos",
    "juguetes-aire-libre": "Juguetes de aire libre",
    "peluches": "Peluches",
    "coleccionismo": "Coleccionismo",
    "anime-y-manga": "Anime y manga",
    "magia-y-trucos": "Magia y trucos",
    # culture
    "lectura": "Lectura",
    "cine-y-series": "Cine y series",
    "musica": "Música",
    "instrumentos": "Tocar un instrumento",
    "fotografia": "Fotografía",
    "escritura": "Escritura",
    "papeleria": "Papelería",
    "idiomas": "Idiomas",
    # sport & outdoors
    "fitness": "Fitness y gimnasio",
    "running": "Running",
    "ciclismo": "Ciclismo",
    "yoga": "Yoga y pilates",
    "senderismo": "Senderismo",
    "camping": "Camping",
    "playa-y-piscina": "Playa y piscina",
    "deportes-equipo": "Deportes de equipo",
    "futbol": "Fútbol",
    "pesca": "Pesca",
    "viajes": "Viajes",
    # tech
    "tecnologia": "Tecnología",
    "informatica": "Informática",
    "domotica": "Casa inteligente",
    "audio-y-sonido": "Audio y sonido",
    "gaming-setup": "Setup gaming",
    "ciencia": "Ciencia",
    "astronomia": "Astronomía",
    "drones": "Drones",
    # personal
    "belleza": "Belleza",
    "cuidado-personal": "Cuidado personal",
    "bienestar-y-relax": "Bienestar y relax",
    "moda-y-complementos": "Moda y complementos",
    "joyeria": "Joyería",
    "relojes": "Relojes",
    # life
    "mascotas": "Mascotas",
    "perros": "Perros",
    "gatos": "Gatos",
    "coche": "Coche",
    "moto": "Moto",
    "oficina-y-teletrabajo": "Oficina y teletrabajo",
    "estudio": "Estudio",
    "crianza": "Crianza y bebés",
    "naturaleza": "Naturaleza",
    "sostenibilidad": "Sostenibilidad",
}


OCCASION_KEYS: list[str] = list(OCCASIONS)
RECIPIENT_KEYS: list[str] = list(RECIPIENTS)
INTEREST_KEYS: list[str] = list(INTERESTS)


def clean(keys: list[str] | None, allowed: dict[str, str], *, limit: int) -> list[str]:
    """Keep only known keys, de-duplicated, order preserved, capped at `limit`.

    Structured outputs already constrain the model to the enum, but a hallucinated or renamed key
    must never reach an ArrayField that search filters against.
    """
    seen: list[str] = []
    for raw in keys or []:
        key = str(raw).strip().lower()
        if key in allowed and key not in seen:
            seen.append(key)
        if len(seen) >= limit:
            break
    return seen


def label(key: str) -> str:
    """Human label for any key in any of the three vocabularies."""
    return OCCASIONS.get(key) or RECIPIENTS.get(key) or INTERESTS.get(key) or key
