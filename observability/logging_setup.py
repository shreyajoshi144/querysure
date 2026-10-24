"""Structured JSON logging with correlation + PII scrubbing (Sprint 4).

One JSON object per line on stderr (unchanged stream: stdout stays clean for tools that parse it):
    {"ts": "...Z", "level": "INFO", "logger": "querysure", "msg": "...", "request_id": "...", "user_ref": "...", ...}

* ``request_id`` / ``user_ref`` / ``session_ref`` / ``job_id`` come from the correlation context, so every line emitted while
  serving a request (or running a job) can be tied together without logging an id or an e-mail address.
* Extra fields passed with ``logger.info("...", extra={"event": "x", "latency_ms": 12})`` are emitted as top-level keys after
  scrubbing; deny-listed keys (sql, question, prompt, email, ...) are replaced by ``[redacted]``.
* Exceptions are reduced to ``exc_type`` + a frame list (``path:function:line``). The exception MESSAGE is omitted unless
  LOG_EXCEPTION_MESSAGES=1 (debug only): messages embed user content that pattern scrubbing cannot reliably remove.
  Source lines and local variables are never included.
* ``LOG_FORMAT=text`` keeps the original one-line format (scrubbed) for local development.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone

import config
from observability import context
from observability.redaction import DENY_KEYS, REDACTED, scrub_text, scrub_value

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HANDLER_TAG = "_qs_obs_handler"

# Attributes every LogRecord has; anything else came from ``extra=``.
_STANDARD = frozenset(logging.LogRecord("x", 0, "x", 0, "", (), None).__dict__) | {"message", "asctime", "taskName"}


def safe_frames(tb, limit: int = 15) -> list[str]:
    """``path:function:line`` for each frame -- no source text, no locals. Project files are root-relative."""
    out = []
    for fs in traceback.extract_tb(tb)[-limit:]:
        path = fs.filename
        if path.startswith(_ROOT + os.sep):
            path = os.path.relpath(path, _ROOT)
        else:
            parts = path.replace("\\", "/").split("/")
            path = "/".join(parts[-2:])
        out.append(f"{path}:{fs.name}:{fs.lineno}")
    return out


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = context.current()
        doc: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": scrub_text(record.getMessage()),
        }
        if ctx is not None:
            for key in ("request_id", "user_ref", "session_ref", "job_id"):
                value = getattr(ctx, key, None)
                if value:
                    doc[key] = value
        for key, value in record.__dict__.items():
            if key in _STANDARD or key in doc or key.startswith("_"):
                continue
            doc[key] = REDACTED if key.lower() in DENY_KEYS else scrub_value(value)
        if record.exc_info and record.exc_info[0] is not None:
            etype, evalue, tb = record.exc_info
            doc["exc_type"] = etype.__name__
            if config.LOG_EXCEPTION_MESSAGES:
                doc["exc_msg"] = scrub_text(evalue, 300)
            doc["stack"] = safe_frames(tb)
        return json.dumps(doc, separators=(",", ":"), default=str)


class TextFormatter(logging.Formatter):
    """Original development format, still scrubbed (and exceptions still reduced to type + frames)."""

    def __init__(self):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        saved = record.msg, record.args
        record.msg, record.args = scrub_text(record.getMessage()), ()
        try:
            base = super().format(record)
        finally:
            record.msg, record.args = saved
        return base

    def formatException(self, ei) -> str:  # noqa: N802 - logging API name
        etype, evalue, tb = ei
        message = f": {scrub_text(evalue, 300)}" if config.LOG_EXCEPTION_MESSAGES else ""
        return f"{etype.__name__}{message}\n  at " + " <- ".join(reversed(safe_frames(tb)))


class UvicornAccessFilter(logging.Filter):
    """uvicorn's access line carries the client address and the full URL including the query string (verification and
    reset tokens travel in query strings). Drop the address and the query string; the app logs its own request line."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5:
            client, method, full_path, http_version, status = args
            record.args = ("-", method, str(full_path).split("?", 1)[0], http_version, status)
        return True


def configure_logging(force: bool = False) -> None:
    """Install the JSON (or text) handler on the root logger. Idempotent; safe to call from api.py and worker.py."""
    root = logging.getLogger()
    if not force and any(getattr(h, _HANDLER_TAG, False) for h in root.handlers):
        return
    for h in [h for h in root.handlers if getattr(h, _HANDLER_TAG, False)]:
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if config.LOG_FORMAT == "json" else TextFormatter())
    setattr(handler, _HANDLER_TAG, True)
    root.addHandler(handler)
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        for h in lg.handlers:  # uvicorn installs its own handlers; route them through our formatter
            h.setFormatter(handler.formatter)
        if name == "uvicorn.access" and not any(isinstance(f, UvicornAccessFilter) for f in lg.filters):
            lg.addFilter(UvicornAccessFilter())
