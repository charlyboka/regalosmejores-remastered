from django.conf import settings
from django.http import HttpRequest


def site(request: HttpRequest) -> dict:
    """Values every template needs (site name, canonical URL, legal disclosure)."""
    return {
        "SITE_NAME": "Regalos Mejores",
        "SITE_URL": settings.SITE_URL,
        "CANONICAL_URL": settings.SITE_URL + request.path,
        "AMAZON_IMAGE_CDN": settings.AMAZON_IMAGE_CDN,
    }
