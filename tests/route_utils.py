"""Flatten an app's routes across FastAPI versions.

FastAPI >= 0.14x no longer copies an included APIRouter's routes into ``app.routes``; it adds one wrapper object that
keeps the original router (``original_router``) plus the include-time context (prefix, dependencies). Security tests
must still see every real route, so this walks those wrappers and returns ``(route, path, extra_dependencies)``.
"""
from fastapi.routing import APIRoute


def iter_api_routes(routes, prefix: str = "", extra_deps=()):
    for r in routes:
        if isinstance(r, APIRoute):
            yield r, prefix + r.path, tuple(extra_deps)
            continue
        original = getattr(r, "original_router", None)
        if original is not None:
            ctx = getattr(r, "include_context", None)
            yield from iter_api_routes(original.routes, prefix + (getattr(ctx, "prefix", "") or ""),
                                       tuple(extra_deps) + tuple(getattr(ctx, "dependencies", None) or ()))
