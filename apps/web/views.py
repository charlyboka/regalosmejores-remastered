from django.http import HttpRequest, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache


@never_cache
def healthz(request: HttpRequest) -> JsonResponse:
    """Liveness probe used by the Heroku deploy health check.

    Deliberately does not touch the database: it must answer even when Postgres is
    unreachable, otherwise a DB blip would roll back an otherwise healthy release.
    """
    return JsonResponse({"status": "ok"})


def home(request: HttpRequest):
    return render(request, "web/home.html")
