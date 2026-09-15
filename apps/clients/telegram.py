"""Telegram notification client.

Contract: ``notify()`` **never raises**. A broken notification channel must never take down a
pipeline. Failures are logged and recorded, nothing more.

Throttling is per ``key``, backed by ``NotificationLog``, so a pipeline that fails every minute
produces one message per throttle window instead of 1,440 a day.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx
from django.conf import settings
from django.utils import timezone

from apps.pipelines.models import NotificationLevel, NotificationLog

logger = logging.getLogger(__name__)

API_BASE_URL = "https://api.telegram.org"
REQUEST_TIMEOUT_SECONDS = 15
MAX_MESSAGE_CHARS = 4000  # Telegram's hard limit is 4096; leave room for the prefix.

LEVEL_PREFIX = {
    NotificationLevel.INFO: "INFO",
    NotificationLevel.WARNING: "AVISO",
    NotificationLevel.ERROR: "ERROR",
    NotificationLevel.CRITICAL: "CRITICO",
}


class TelegramClient:
    def __init__(self, *, bot_token: str | None = None, channel_id: str | None = None) -> None:
        self.bot_token = bot_token if bot_token is not None else settings.TELEGRAM_BOT_TOKEN
        self.channel_id = channel_id if channel_id is not None else settings.TELEGRAM_CHANNEL_ID

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.channel_id)

    def notify(
        self,
        key: str,
        message: str,
        *,
        level: str = NotificationLevel.INFO,
        throttle_minutes: int = 60,
    ) -> bool:
        """Post to the channel. Returns True only if a message was actually sent."""
        try:
            return self._notify(key, message, level=level, throttle_minutes=throttle_minutes)
        except Exception:  # noqa: BLE001 - notifications must never break a caller
            logger.exception("Telegram notification %r failed", key)
            return False

    # --- internals -----------------------------------------------------------

    def _notify(self, key: str, message: str, *, level: str, throttle_minutes: int) -> bool:
        if not self.is_configured:
            logger.warning("Telegram not configured; dropping notification %r: %s", key, message)
            return False

        if throttle_minutes > 0 and self._recently_sent(key, throttle_minutes):
            NotificationLog.objects.create(
                key=key, level=level, message=message[:MAX_MESSAGE_CHARS], suppressed=True
            )
            logger.info("Telegram notification %r throttled", key)
            return False

        text = self._format(level, message)
        response = httpx.post(
            f"{API_BASE_URL}/bot{self.bot_token}/sendMessage",
            json={
                "chat_id": self.channel_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            logger.error(
                "Telegram sendMessage failed (HTTP %s): %s",
                response.status_code,
                response.text[:300],
            )
            return False

        NotificationLog.objects.create(
            key=key, level=level, message=message[:MAX_MESSAGE_CHARS], suppressed=False
        )
        return True

    @staticmethod
    def _recently_sent(key: str, throttle_minutes: int) -> bool:
        since = timezone.now() - timedelta(minutes=throttle_minutes)
        return NotificationLog.objects.filter(
            key=key, suppressed=False, sent_at__gte=since
        ).exists()

    @staticmethod
    def _format(level: str, message: str) -> str:
        prefix = LEVEL_PREFIX.get(level, str(level).upper())
        body = message[:MAX_MESSAGE_CHARS]
        return f"<b>[{prefix}] Regalos Mejores</b>\n{body}"


def notify(
    key: str,
    message: str,
    *,
    level: str = NotificationLevel.INFO,
    throttle_minutes: int = 60,
) -> bool:
    """Module-level shortcut so callers don't have to instantiate the client."""
    return TelegramClient().notify(key, message, level=level, throttle_minutes=throttle_minutes)
