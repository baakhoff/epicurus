"""Introspect the URL paths a FastAPI app (or router) mounts.

FastAPI 0.137 changed ``include_router`` to attach a lazy ``_IncludedRouter``
wrapper to ``app.routes`` instead of eagerly flattening the included routes into
it. The wrapper carries no ``.path``, so the previous idiom
``[r.path for r in app.routes]`` stops seeing nested routes (it yields ``""`` for
each wrapper and misses ``/health``, ``/metrics``, the platform routes, …).

:func:`route_paths` flattens the tree by recursing into each wrapper's
``original_router``, returning the full set of paths on both old and new
FastAPI. Plain routes and ASGI mounts (e.g. ``/mcp``) keep their own ``.path``.
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

__all__ = ["route_paths"]


def route_paths(app: FastAPI | APIRouter) -> list[str]:
    """Every URL path mounted on *app*, flattened across included routers.

    Order follows ``app.routes``; duplicates are not removed (callers test with
    ``in`` / ``startswith``). Safe on FastAPI < 0.137 too, where included routes
    are already flat and simply expose their ``.path``.
    """
    paths: list[str] = []
    for route in app.routes:
        included = getattr(route, "original_router", None)  # FastAPI 0.137 lazy include
        if included is not None:
            paths.extend(route_paths(included))
            continue
        path = getattr(route, "path", None)
        if path is not None:
            paths.append(path)
    return paths
