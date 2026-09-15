import logging
import os
import sys
import threading

from django.apps import AppConfig

logger = logging.getLogger(__name__)


class SearchConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.search"
    label = "search"
    verbose_name = "Búsqueda"

    def ready(self) -> None:
        _warm_embedding_client()


def _warm_embedding_client() -> None:
    """Open the OpenAI connection at boot so the first real visitor doesn't pay for it.

    Establishing the TLS connection measured 6.9 s cold against 375 ms warm. Doing it in a
    background thread at startup costs one embedding call (~$0.0000002 per dyno boot) and keeps
    that latency off the request path entirely.
    """
    if not _should_warm():
        return

    def _warm() -> None:
        try:
            from apps.search.engine import _shared_client

            _shared_client().embed_one("regalo", purpose="warmup")
        except Exception as exc:  # never let a warmup failure break startup
            logger.warning("No se pudo precalentar el cliente de embeddings: %s", exc)

    threading.Thread(target=_warm, name="embed-warmup", daemon=True).start()


def _should_warm() -> bool:
    """Web processes only. Migrations, shells and one-shot commands would just waste a call."""
    if os.environ.get("DISABLE_SEARCH_WARMUP"):
        return False
    argv = " ".join(sys.argv)
    return "gunicorn" in argv or "runserver" in argv
