"""Safe error tracking (Sprint 4).

What is recorded for an exception: its *type*, a stable fingerprint, the component, and a ``path:function`` location of the
innermost project frame. What is NOT recorded: the exception message (driver/SDK/validation errors routinely embed row values,
e-mail addresses, SQL and request fragments), request bodies/headers, local variables, source lines, user identity.

* Aggregated per fingerprint (count / first / last seen) in Redis (shared by all processes) with in-memory fallback -- this
  is what the admin dashboard shows.
* The *log line* carries a scrubbed, truncated message for the operator (logs are access-controlled infrastructure, the
  dashboard is a product surface).
* Optional Sentry forwarding (``SENTRY_DSN``): strict ``before_send`` scrubber that drops messages, request, user, headers,
  breadcrumbs and frame variables. Sentry is not a hard dependency; it is imported only when a DSN is configured.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from typing import Optional, Tuple

import config
from observability import metrics
from observability.logging_setup import safe_frames
from observability.store import get_store

logger = logging.getLogger("querysure.errors")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REDIS_META, _REDIS_COUNT = "qs:err:meta", "qs:err:count"
_TTL = 7 * 24 * 3600
_MAX_FINGERPRINTS = 200
_mem: dict = {}
_mem_lock = threading.Lock()
_sentry_ready = False


def fingerprint(exc: BaseException) -> Tuple[str, str]:
    """(fingerprint, location). Location = innermost project frame as ``relpath:function`` (line numbers excluded so a
    deploy that shifts lines does not split the group)."""
    location = "unknown"
    tb = exc.__traceback__
    frames = []
    while tb is not None:
        frames.append((tb.tb_frame.f_code.co_filename, tb.tb_frame.f_code.co_name))
        tb = tb.tb_next
    for filename, func in reversed(frames):
        if filename.startswith(_ROOT + os.sep) and "site-packages" not in filename:
            location = f"{os.path.relpath(filename, _ROOT)}:{func}"
            break
    else:
        if frames:
            location = f"{os.path.basename(frames[-1][0])}:{frames[-1][1]}"
    digest = hashlib.sha1(f"{type(exc).__name__}|{location}".encode("utf-8")).hexdigest()[:12]
    return digest, location


def capture_exception(exc: BaseException, *, component: str = "api", request_id: Optional[str] = None) -> Optional[str]:
    """Record an exception safely. Returns its fingerprint. Never raises."""
    try:
        fp, location = fingerprint(exc)
        etype = type(exc).__name__
        logger.error("captured exception", exc_info=(type(exc), exc, exc.__traceback__),
                     extra={"event": "exception_captured", "fingerprint": fp, "component": component, "location": location})
        metrics.record_error(component, etype)
        _aggregate(fp, etype, component, location)
        _forward_to_sentry(exc, fp, component)
        return fp
    except Exception:  # noqa: BLE001
        return None


def _aggregate(fp: str, etype: str, component: str, location: str) -> None:
    now = time.time()
    client = None
    try:
        client = get_store().redis_client() if get_store().healthy() else None
    except Exception:  # noqa: BLE001
        client = None
    if client is not None:
        try:
            if client.hlen(_REDIS_META) < _MAX_FINGERPRINTS or client.hexists(_REDIS_META, fp):
                prior = client.hget(_REDIS_META, fp)
                first = json.loads(prior)["first"] if prior else now
                client.hset(_REDIS_META, fp, json.dumps({"type": etype, "component": component, "location": location,
                                                          "first": first, "last": now}))
                client.hincrby(_REDIS_COUNT, fp, 1)
                client.expire(_REDIS_META, _TTL)
                client.expire(_REDIS_COUNT, _TTL)
            return
        except Exception:  # noqa: BLE001
            pass
    with _mem_lock:
        entry = _mem.get(fp)
        if entry is None and len(_mem) >= _MAX_FINGERPRINTS:
            return
        if entry is None:
            entry = _mem[fp] = {"type": etype, "component": component, "location": location, "first": now, "count": 0}
        entry["count"] += 1
        entry["last"] = now


def top_errors(limit: int = 20) -> list:
    """Most frequent fingerprints (type + location + counts; never a message)."""
    merged: dict = {}
    with _mem_lock:
        for fp, e in _mem.items():
            merged[fp] = dict(e)
    client = None
    try:
        client = get_store().redis_client() if get_store().healthy() else None
    except Exception:  # noqa: BLE001
        client = None
    if client is not None:
        try:
            counts = client.hgetall(_REDIS_COUNT)
            for fp_raw, raw in client.hgetall(_REDIS_META).items():
                fp = fp_raw.decode() if isinstance(fp_raw, bytes) else fp_raw
                meta = json.loads(raw)
                c = counts.get(fp_raw)
                meta["count"] = merged.get(fp, {}).get("count", 0) + int(c or 0)
                merged[fp] = meta
        except Exception:  # noqa: BLE001
            pass
    rows = [{"fingerprint": fp, "type": e["type"], "component": e["component"], "location": e["location"],
             "count": e["count"], "first_seen": e["first"], "last_seen": e["last"]} for fp, e in merged.items()]
    rows.sort(key=lambda r: (-r["count"], -r["last_seen"]))
    return rows[:limit]


def reset() -> None:
    with _mem_lock:
        _mem.clear()


# ---------------------------------------------------------------------------------------------------------------
# Optional Sentry
# ---------------------------------------------------------------------------------------------------------------
def scrub_sentry_event(event: dict, hint=None) -> dict:
    """``before_send`` hook: reduce an event to type + frames + fingerprint tags. Pure function (tested without sentry)."""
    out: dict = {"level": event.get("level", "error"), "platform": event.get("platform", "python"),
                 "tags": {k: v for k, v in (event.get("tags") or {}).items() if k in ("component", "fingerprint")}}
    # The SDK itself requires these after before_send (it indexes event["event_id"]); dropping them makes it raise internally
    # and silently discard the event. Both are generated ids / timestamps, not user data.
    for required in ("event_id", "timestamp", "environment"):
        if event.get(required) is not None:
            out[required] = event[required]
    values = []
    for exc in ((event.get("exception") or {}).get("values") or []):
        frames = []
        for fr in ((exc.get("stacktrace") or {}).get("frames") or []):
            path = str(fr.get("filename") or "")
            frames.append({"filename": path[-120:], "function": fr.get("function"), "lineno": fr.get("lineno")})
        values.append({"type": exc.get("type"), "value": "[omitted]", "stacktrace": {"frames": frames}})
    if values:
        out["exception"] = {"values": values}
    return out  # request, user, extra, contexts, breadcrumbs, server_name, modules, sdk-added PII: all dropped


def init_sentry() -> bool:
    """Initialise Sentry if SENTRY_DSN is set and the SDK is installed. Returns True when active."""
    global _sentry_ready
    if _sentry_ready or not config.SENTRY_DSN:
        return _sentry_ready
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=config.SENTRY_DSN, environment=config.SENTRY_ENVIRONMENT, send_default_pii=False,
                        default_integrations=False, auto_enabling_integrations=False, max_breadcrumbs=0,
                        include_local_variables=False, include_source_context=False, attach_stacktrace=False,
                        traces_sample_rate=0.0, before_send=scrub_sentry_event)
        _sentry_ready = True
    except Exception:  # noqa: BLE001
        logger.warning("SENTRY_DSN is set but sentry-sdk could not be initialised", extra={"event": "sentry_init_failed"})
    return _sentry_ready


def _forward_to_sentry(exc: BaseException, fp: str, component: str) -> None:
    if not _sentry_ready:
        return
    try:
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("component", component)
            scope.set_tag("fingerprint", fp)
            scope.fingerprint = [fp]
            sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001
        pass


__all__ = ["capture_exception", "top_errors", "fingerprint", "scrub_sentry_event", "init_sentry", "safe_frames", "reset"]
