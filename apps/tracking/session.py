"""Cookieless visitor identification.

No cookie is set and nothing personal is stored. The identifier is a salted HMAC of the IP and
user agent that **rotates every day**, so it correlates one visitor's page views within a single
day and becomes meaningless afterwards. Neither the IP nor the user agent is ever written to the
database, and the hash cannot be reversed to recover them.

This is why the site needs no consent banner: there is no cookie and no cross-day identifier.
"""

from __future__ import annotations

from django.http import HttpRequest
from django.utils import timezone
from django.utils.crypto import salted_hmac

# Matched against a lowercased user agent. Deliberately broad: a false positive only means a
# visit is excluded from the figures, which is far better than inflating them with crawlers.
BOT_MARKERS = (
    "bot",
    "crawl",
    "spider",
    "slurp",
    "curl",
    "wget",
    "python-requests",
    "httpx",
    "headless",
    "lighthouse",
    "pingdom",
    "uptime",
    "monitor",
    "scrapy",
    "facebookexternalhit",
    "preview",
    "fetcher",
    "archiver",
    "validator",
)


def visitor_hash(request: HttpRequest) -> str:
    """A stable-for-one-day, non-reversible identifier for this visitor."""
    material = f"{client_ip(request)}|{user_agent(request)}"
    key_salt = f"regalosmejores.visitor.{timezone.now():%Y-%m-%d}"
    return salted_hmac(key_salt, material).hexdigest()[:32]


def user_agent(request: HttpRequest) -> str:
    return request.META.get("HTTP_USER_AGENT", "")[:400]


def client_ip(request: HttpRequest) -> str:
    """Best-effort client IP, used only as hash material.

    ``X-Forwarded-For`` is client-controlled and therefore spoofable, but this value never
    reaches an authentication, authorisation or rate-limiting decision. Spoofing it only
    fragments one visitor's own analytics rows.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.META.get("REMOTE_ADDR", "")[:45]


def is_bot(request: HttpRequest) -> bool:
    agent = user_agent(request).lower()
    if not agent:
        return True  # A real browser always sends one.
    return any(marker in agent for marker in BOT_MARKERS)
