"""Durable storage for report artifacts (Sprint 3). A NEW module: storage.py's public API is untouched; this
reuses its S3 client, bucket and key prefix rules.

Key layout (both backends):  <prefix>/reports/<user_id>/<uuid>
  s3    -> object in the configured bucket (storage.BUCKET), uploaded with storage's own client.
  local -> the same relative key under <parent of UPLOAD_DIR>/reports/ (development only).

Keys are generated here from validated pieces and re-validated on every read, so a tampered ``reports.key``
can never make the server open an arbitrary object or file.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import uuid
from typing import Iterator, Optional, Tuple

import storage

logger = logging.getLogger("querysure")

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# kind -> (content type, download filename)
KINDS = {
    "json": ("application/json", "report.json"),
    "html": ("text/html; charset=utf-8", "report.html"),
    "pdf": ("application/pdf", "report.pdf"),
    "rows_csv": ("text/csv; charset=utf-8", "report_rows.csv"),
    "summary_csv": ("text/csv; charset=utf-8", "report_summary.csv"),
}


def _is_remote() -> bool:
    return storage.BACKEND == "s3"


def _key_pattern(user_id: Optional[str] = None) -> re.Pattern:
    uid = re.escape(user_id) if user_id else _UUID
    return re.compile(rf"^{re.escape(storage.PREFIX)}/reports/{uid}/{_UUID}$")


def new_key(user_id: str) -> str:
    return f"{storage.PREFIX}/reports/{user_id}/{uuid.uuid4()}"


def _local_root() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(storage.UPLOAD_DIR)), "reports")


def _local_path(key: str, user_id: Optional[str] = None) -> str:
    if not _key_pattern(user_id).match(key or ""):
        raise storage.DatasetUnavailable("Malformed report reference.")
    root = os.path.realpath(_local_root())
    path = os.path.realpath(os.path.join(root, *key.split("/")))
    if os.path.commonpath([root, path]) != root:
        raise storage.DatasetUnavailable("Malformed report reference.")
    return path


def store_report_file(local_path: str, user_id: str, kind: str) -> Tuple[str, int]:
    """Copy one finished artifact into durable storage. Returns (key, size_bytes). Raises StorageError on failure
    (nothing is left behind). The caller deletes its temp file afterwards."""
    if kind not in KINDS:
        raise ValueError(f"unknown report kind {kind!r}")
    size = os.path.getsize(local_path)
    key = new_key(user_id)
    if _is_remote():
        try:
            storage._client().upload_file(local_path, storage.BUCKET, key, ExtraArgs={"ContentType": KINDS[kind][0]})
        except storage._client_error_types() as e:
            logger.error("report upload to s3 failed: %s", e)
            raise storage.StorageError(str(e)) from e
    else:
        dest = _local_path(key, user_id)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        try:
            shutil.copyfile(local_path, dest)
        except OSError as e:
            raise storage.StorageError(str(e)) from e
    return key, size


def delete_report_object(key: Optional[str]) -> bool:
    """Best-effort delete (used to roll back a half-finished report job)."""
    try:
        if not key:
            return False
        if _is_remote():
            if not _key_pattern().match(key):
                return False
            storage._client().delete_object(Bucket=storage.BUCKET, Key=key)
            return True
        path = _local_path(key)
        if os.path.isfile(path):
            os.remove(path)
            return True
    except Exception as e:  # noqa: BLE001
        logger.warning("report object cleanup failed: %s", e)
    return False


def open_report(key: str, user_id: str) -> Tuple[Iterator[bytes], Optional[int]]:
    """Return (chunk iterator, content_length) for a report object belonging to ``user_id``.
    Raises storage.DatasetUnavailable when the key is malformed, foreign, or the object is gone."""
    if not _key_pattern(user_id).match(key or ""):
        raise storage.DatasetUnavailable("Report does not belong to this user.")
    if _is_remote():
        try:
            obj = storage._client().get_object(Bucket=storage.BUCKET, Key=key)
        except storage._client_error_types() as e:
            raise storage.DatasetUnavailable("Report object is unavailable.") from e
        return obj["Body"].iter_chunks(64 * 1024), obj.get("ContentLength")
    path = _local_path(key, user_id)
    if not os.path.isfile(path):
        raise storage.DatasetUnavailable("Report object is unavailable.")

    def _iter() -> Iterator[bytes]:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(64 * 1024)
                if not chunk:
                    return
                yield chunk
    return _iter(), os.path.getsize(path)
