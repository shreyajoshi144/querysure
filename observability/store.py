"""Aggregate metrics store: per-minute buckets of counters and latency histograms (Sprint 4).

* Shared across processes through Redis (``qs:m:<minute>`` hashes, TTL = METRICS_RETENTION_SECONDS) so the web workers and
  the RQ worker all feed one view; falls back to process-local memory when Redis is absent or failing. Writes are buffered
  and flushed by a daemon thread, so Redis latency/outage can never slow or break a request.
* Only bounded label sets are stored (route template, status class, provider, outcome...). There is no per-user, per-request
  or free-text series -- this is what makes the admin dashboard privacy-safe by construction.
* Latency percentiles are estimated from fixed histogram buckets (linear interpolation inside a bucket), i.e. accurate to the
  bucket resolution, not exact. Bounds are in ms.
"""
from __future__ import annotations

import bisect
import logging
import os
import re
import threading
import time
from collections import defaultdict
from typing import Callable, Dict, Iterable, Optional, Tuple

import config

logger = logging.getLogger("querysure.metrics")

BUCKET_SECONDS = 60
HIST_BOUNDS_MS: Tuple[float, ...] = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 20000, 30000, 60000)
_REDIS_COOLDOWN_SECONDS = 15.0
_BAD_LABEL_CHARS = re.compile(r"[|,=\s]")


def _clean(value) -> str:
    return _BAD_LABEL_CHARS.sub("_", str(value))[:48]


def labels_key(labels: dict) -> str:
    return ",".join(f"{k}={_clean(v)}" for k, v in sorted(labels.items()))


def _parse_labels(key: str) -> Tuple[Tuple[str, str], ...]:
    if not key:
        return ()
    return tuple(tuple(part.split("=", 1)) for part in key.split(","))  # type: ignore[misc]


def quantile_from_buckets(counts: list, q: float, bounds: Tuple[float, ...] = HIST_BOUNDS_MS) -> Optional[float]:
    """Estimate quantile ``q`` (0..1) from non-cumulative bucket counts (len(bounds)+1 entries; last = overflow)."""
    total = sum(counts)
    if total <= 0:
        return None
    rank = q * total
    seen = 0.0
    for i, c in enumerate(counts):
        if c <= 0:
            continue
        if seen + c >= rank:
            if i >= len(bounds):                      # overflow bucket: all we know is "above the last bound"
                return float(bounds[-1])
            lower = bounds[i - 1] if i > 0 else 0.0
            upper = bounds[i]
            return lower + (upper - lower) * ((rank - seen) / c)
        seen += c
    return float(bounds[-1])


class Snapshot:
    """Merged view of N minute-buckets."""

    def __init__(self, window_minutes: int):
        self.window_minutes = window_minutes
        self.counters: Dict[Tuple[str, Tuple], float] = defaultdict(float)
        self.hists: Dict[Tuple[str, Tuple], dict] = {}

    # -- building --------------------------------------------------------------------------------------------
    def add_field(self, field: str, value: float) -> None:
        parts = field.split("|")
        kind, name = parts[0], parts[1]
        labels = _parse_labels(parts[2] if len(parts) > 2 else "")
        if kind == "c":
            self.counters[(name, labels)] += value
            return
        h = self.hists.setdefault((name, labels), {"b": [0.0] * (len(HIST_BOUNDS_MS) + 1), "sum": 0.0, "n": 0.0})
        if kind == "hb":
            idx = int(parts[3])
            if 0 <= idx < len(h["b"]):
                h["b"][idx] += value
        elif kind == "hs":
            h["sum"] += value
        elif kind == "hn":
            h["n"] += value

    # -- querying --------------------------------------------------------------------------------------------
    @staticmethod
    def _match(labels: Tuple, flt: dict) -> bool:
        if not flt:
            return True
        d = dict(labels)
        return all(d.get(k) == _clean(v) for k, v in flt.items())

    def counter(self, name: str, **flt) -> float:
        return sum(v for (n, lb), v in self.counters.items() if n == name and self._match(lb, flt))

    def counter_by(self, name: str, label: str, **flt) -> Dict[str, float]:
        out: Dict[str, float] = defaultdict(float)
        for (n, lb), v in self.counters.items():
            if n == name and self._match(lb, flt):
                out[dict(lb).get(label, "")] += v
        return dict(out)

    def _merged(self, name: str, flt: dict) -> Optional[dict]:
        merged = None
        for (n, lb), h in self.hists.items():
            if n == name and self._match(lb, flt):
                if merged is None:
                    merged = {"b": [0.0] * len(h["b"]), "sum": 0.0, "n": 0.0}
                merged["b"] = [a + b for a, b in zip(merged["b"], h["b"])]
                merged["sum"] += h["sum"]
                merged["n"] += h["n"]
        return merged

    def quantile(self, name: str, q: float, **flt) -> Optional[float]:
        m = self._merged(name, flt)
        return quantile_from_buckets(m["b"], q) if m else None

    def hist_count(self, name: str, **flt) -> float:
        m = self._merged(name, flt)
        return m["n"] if m else 0.0

    def hist_mean(self, name: str, **flt) -> Optional[float]:
        m = self._merged(name, flt)
        return (m["sum"] / m["n"]) if m and m["n"] else None

    def hist_label_values(self, name: str, label: str) -> list:
        return sorted({dict(lb).get(label, "") for (n, lb) in self.hists if n == name})

    def latency_summary(self, name: str, **flt) -> dict:
        return {"count": int(self.hist_count(name, **flt)), "mean_ms": _r(self.hist_mean(name, **flt)),
                "p50_ms": _r(self.quantile(name, 0.50, **flt)), "p95_ms": _r(self.quantile(name, 0.95, **flt)),
                "p99_ms": _r(self.quantile(name, 0.99, **flt))}


def _r(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 1)


class MetricsStore:
    def __init__(self, redis_url: Optional[str] = None, redis_factory: Optional[Callable] = None,
                 clock: Callable[[], float] = time.time, retention_seconds: Optional[int] = None,
                 autoflush: bool = True, flush_interval: Optional[float] = None):
        self._redis_url = redis_url
        self._redis_factory = redis_factory
        self._client = None
        self._clock = clock
        self._retention = retention_seconds or config.METRICS_RETENTION_SECONDS
        self._interval = flush_interval or config.METRICS_FLUSH_INTERVAL_SECONDS
        self._autoflush = autoflush
        self._lock = threading.Lock()
        self._pending: Dict[int, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self._local: Dict[int, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
        self._down_until = 0.0
        self._thread: Optional[threading.Thread] = None
        self._thread_pid: Optional[int] = None
        self.redis_errors = 0

    # -- config ----------------------------------------------------------------------------------------------
    @property
    def uses_redis(self) -> bool:
        return bool(self._redis_url or self._redis_factory)

    def _redis(self):
        if self._client is None:
            if self._redis_factory:
                self._client = self._redis_factory()
            else:
                import redis
                self._client = redis.Redis.from_url(self._redis_url, socket_connect_timeout=1, socket_timeout=1)
        return self._client

    def redis_client(self):
        """The underlying client (for sibling modules that share the connection settings) or None."""
        return self._redis() if self.uses_redis else None

    def _bucket(self) -> int:
        return int(self._clock() // BUCKET_SECONDS)

    # -- recording -------------------------------------------------------------------------------------------
    def _target(self) -> Dict[int, Dict[str, float]]:
        return self._pending if self.uses_redis else self._local

    def inc(self, name: str, value: float = 1.0, **labels) -> None:
        if not config.METRICS_ENABLED:
            return
        field = f"c|{name}|{labels_key(labels)}"
        with self._lock:
            self._target()[self._bucket()][field] += value
        self._ensure_thread()

    def observe(self, name: str, value_ms: float, **labels) -> None:
        if not config.METRICS_ENABLED or value_ms is None:
            return
        lk = labels_key(labels)
        idx = bisect.bisect_left(HIST_BOUNDS_MS, value_ms)
        with self._lock:
            b = self._target()[self._bucket()]
            b[f"hb|{name}|{lk}|{idx}"] += 1
            b[f"hs|{name}|{lk}"] += float(value_ms)
            b[f"hn|{name}|{lk}"] += 1
        self._ensure_thread()

    def _ensure_thread(self) -> None:
        if not (self.uses_redis and self._autoflush):
            return
        if self._thread is not None and self._thread_pid == os.getpid():
            return
        with self._lock:
            if self._thread is not None and self._thread_pid == os.getpid():
                return
            # first use, or we are a forked child (RQ work horse): threads do not survive fork, start our own
            t = threading.Thread(target=self._loop, name="qs-metrics-flush", daemon=True)
            self._thread, self._thread_pid = t, os.getpid()
        t.start()

    def _loop(self) -> None:
        while True:
            time.sleep(self._interval)
            try:
                self.flush()
            except Exception:  # noqa: BLE001 - observability must never kill its own thread
                logger.debug("metrics flush failed", exc_info=True)

    # -- flushing --------------------------------------------------------------------------------------------
    def flush(self) -> bool:
        """Push buffered buckets to Redis. On any failure the batch is kept in local memory (nothing is dropped) and
        Redis is skipped for a short cooldown. Returns True when the buffer was delivered (or was empty)."""
        if not self.uses_redis:
            self._prune_local()
            return True
        with self._lock:
            batch, self._pending = self._pending, defaultdict(lambda: defaultdict(float))
        if not batch:
            return True
        if self._clock() < self._down_until:
            self._merge_local(batch)
            return False
        try:
            pipe = self._redis().pipeline(transaction=False)
            for bucket, fields in batch.items():
                key = f"qs:m:{bucket}"
                for field, value in fields.items():
                    pipe.hincrbyfloat(key, field, value)
                pipe.expire(key, self._retention)
            pipe.execute()
            return True
        except Exception:  # noqa: BLE001
            self.redis_errors += 1
            self._down_until = self._clock() + _REDIS_COOLDOWN_SECONDS
            logger.warning("metrics store: Redis unavailable, buffering in memory", extra={"event": "metrics_redis_down"})
            self._merge_local(batch)
            return False

    def _merge_local(self, batch: Dict[int, Dict[str, float]]) -> None:
        with self._lock:
            for bucket, fields in batch.items():
                tgt = self._local[bucket]
                for f, v in fields.items():
                    tgt[f] += v

    def _prune_local(self) -> None:
        cutoff = self._bucket() - self._retention // BUCKET_SECONDS
        with self._lock:
            for b in [b for b in self._local if b < cutoff]:
                del self._local[b]

    # -- reading ---------------------------------------------------------------------------------------------
    def snapshot(self, window_minutes: int = 5) -> Snapshot:
        window_minutes = max(1, int(window_minutes))
        self.flush()
        now_b = self._bucket()
        buckets = list(range(now_b - window_minutes + 1, now_b + 1))
        snap = Snapshot(window_minutes)
        if self.uses_redis and self._clock() >= self._down_until:
            try:
                pipe = self._redis().pipeline(transaction=False)
                for b in buckets:
                    pipe.hgetall(f"qs:m:{b}")
                for h in pipe.execute():
                    for f, v in (h or {}).items():
                        snap.add_field(f.decode() if isinstance(f, bytes) else f, float(v))
            except Exception:  # noqa: BLE001
                self.redis_errors += 1
                self._down_until = self._clock() + _REDIS_COOLDOWN_SECONDS
        with self._lock:
            for b in buckets:
                for f, v in self._local.get(b, {}).items():
                    snap.add_field(f, v)
        return snap

    def healthy(self) -> bool:
        return (not self.uses_redis) or self._clock() >= self._down_until

    def reset(self) -> None:
        """Test hook: drop all in-process state."""
        with self._lock:
            self._pending.clear()
            self._local.clear()
        self._down_until = 0.0


_store: Optional[MetricsStore] = None
_store_lock = threading.Lock()


def get_store() -> MetricsStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = MetricsStore(redis_url=config.REDIS_URL)
    return _store


def set_store(store: Optional[MetricsStore]) -> None:
    """Replace the process-wide store (tests; or after fork if ever needed)."""
    global _store
    _store = store


def iter_prom_lines(snap: Snapshot) -> Iterable[str]:
    """Prometheus text exposition of a snapshot (cumulative histogram buckets, window-scoped values)."""
    seen = set()
    for (name, labels), v in sorted(snap.counters.items()):
        if name not in seen:
            seen.add(name)
            yield f"# TYPE qs_{name}_window counter"
        yield f"qs_{name}_window{_prom_labels(labels)} {v:g}"
    hseen = set()
    for (name, labels), h in sorted(snap.hists.items()):
        if name not in hseen:
            hseen.add(name)
            yield f"# TYPE qs_{name} histogram"
        cum = 0.0
        for bound, c in zip(HIST_BOUNDS_MS, h["b"]):
            cum += c
            yield f'qs_{name}_bucket{_prom_labels(labels, le=f"{bound:g}")} {cum:g}'
        yield f'qs_{name}_bucket{_prom_labels(labels, le="+Inf")} {h["n"]:g}'
        yield f"qs_{name}_sum{_prom_labels(labels)} {h['sum']:g}"
        yield f"qs_{name}_count{_prom_labels(labels)} {h['n']:g}"


def _prom_labels(labels: Tuple, **extra) -> str:
    items = list(labels) + list(extra.items())
    if not items:
        return ""
    return "{" + ",".join(f'{k}="{v}"' for k, v in items) + "}"
