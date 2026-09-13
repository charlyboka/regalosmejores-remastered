"""Pipeline registry with decorator-based auto-discovery.

Every module in ``apps/pipelines/pipelines/`` is imported once on first access; any class
decorated with ``@register`` becomes available by key to the worker, the scheduler and the
``run_pipeline`` command.
"""

from __future__ import annotations

import importlib
import pkgutil

from apps.pipelines.base import Pipeline

_REGISTRY: dict[str, type[Pipeline]] = {}
_DISCOVERED = False

PIPELINE_PACKAGE = "apps.pipelines.pipelines"


class PipelineNotFound(KeyError):
    pass


def register(cls: type[Pipeline]) -> type[Pipeline]:
    if not getattr(cls, "key", ""):
        raise ValueError(f"{cls.__name__} must define a non-empty `key`")
    if cls.key in _REGISTRY and _REGISTRY[cls.key] is not cls:
        raise ValueError(f"Duplicate pipeline key {cls.key!r} ({cls.__name__})")
    _REGISTRY[cls.key] = cls
    return cls


def autodiscover() -> None:
    global _DISCOVERED
    if _DISCOVERED:
        return
    _DISCOVERED = True  # set first so a failing import cannot cause an infinite retry loop
    package = importlib.import_module(PIPELINE_PACKAGE)
    for module in pkgutil.iter_modules(package.__path__):
        if not module.name.startswith("_"):
            importlib.import_module(f"{PIPELINE_PACKAGE}.{module.name}")


def all_pipelines() -> dict[str, type[Pipeline]]:
    autodiscover()
    return dict(sorted(_REGISTRY.items()))


def get_pipeline(key: str) -> Pipeline:
    autodiscover()
    try:
        return _REGISTRY[key]()
    except KeyError as exc:
        known = ", ".join(sorted(_REGISTRY)) or "none registered"
        raise PipelineNotFound(f"Unknown pipeline {key!r}. Known keys: {known}") from exc
