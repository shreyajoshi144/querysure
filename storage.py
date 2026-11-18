"""
Dataset storage.

Every upload (CSV / JSON / XLSX / XLSM / SQLite) ends up as one SQLite file,
and the query engine needs a real local file to open. This module decides
where the *durable* copy of that file lives:

  local backend (development default)
      The file stays where the upload handler wrote it (./uploads/<uuid>.db)
      and `db_path` on the session is that local path -- exactly the
      pre-existing behaviour.

  s3 backend (required in production)
      The file is uploaded to object storage under
      `<prefix>/<user_id>/<uuid>.db` and `db_path` on the session becomes
      `s3://<bucket>/<key>`. The local disk only holds a cache: on first use
      after a restart/redeploy the object is downloaded again, so a session's
      dataset survives the container being replaced.

`db_path` therefore stays a plain string column -- no schema migration -- and
old rows holding local paths keep working.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import uuid
from typing import Optional, Tuple

import config

logger = logging.getLogger("querysure.storage")

ROOT = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(ROOT, "uploads")
CACHE_DIR = os.path.join(UPLOAD_DIR, ".cache")

# Module-level copies of the settings so tests can override them.
BACKEND = config.STORAGE_BACKEND
BUCKET = config.S3_BUCKET
PREFIX = config.S3_PREFIX
REGION = config.S3_REGION
ENDPOINT_URL = config.S3_ENDPOINT_URL
CACHE_MAX_BYTES = config.STORAGE_CACHE_MAX_MB * 1024 * 1024

_UUID = r"[0-9a-fA-F-]{36}"
_S3_REF = re.compile(r"^s3://(?P<bucket>[^/]+)/(?P<key>.+)$")

_client_singleton = None


class StorageError(Exception):
    """The storage backend failed (network, credentials, missing bucket...).
    Usually transient / operator-fixable -- callers should answer 503."""


class DatasetUnavailable(Exception):
    """The dataset this session points at no longer exists (or isn't this
    caller's to read). Callers should tell the user to upload it again."""


# --------------------------------------------------------------------- helpers

def is_remote(ref: Optional[str]) -> bool:
    return bool(ref) and ref.startswith("s3://")


def _reset_client_cache() -> None:  # used by tests
    global _client_singleton
    _client_singleton = None


def _client():
    global _client_singleton
    if _client_singleton is None:
        try:
            import boto3  # lazy: local/dev never needs it
        except ImportError as e:  # pragma: no cover
            raise StorageError("boto3 is not installed but the s3 backend is enabled.") from e
        kwargs = {}
        if REGION:
            kwargs["region_name"] = REGION
        if ENDPOINT_URL:
            kwargs["endpoint_url"] = ENDPOINT_URL
        _client_singleton = boto3.client("s3", **kwargs)
    return _client_singleton


def _client_error_types():
    # upload_file() wraps client failures in boto3's S3UploadFailedError, which
    # is NOT a ClientError/BotoCoreError -- Boto3Error is their common base.
    from boto3.exceptions import Boto3Error
    from botocore.exceptions import BotoCoreError, ClientError
    return (BotoCoreError, ClientError, Boto3Error)


def _parse_remote(ref: str, user_id: Optional[str] = None) -> Tuple[str, str]:
    """Validates an s3:// reference and returns (bucket, key).

    The reference comes from a database row, but it is still re-validated
    here: it must point at *our* bucket, under *our* prefix, in the layout we
    generate, and (when user_id is given) inside that user's own folder. A
    tampered or foreign reference can therefore never make the server read
    someone else's object or an arbitrary key.
    """
    m = _S3_REF.match(ref or "")
    if not m:
        raise DatasetUnavailable("Malformed dataset reference.")
    bucket, key = m.group("bucket"), m.group("key")
    pattern = re.compile(rf"^{re.escape(PREFIX)}/(?P<uid>{_UUID})/{_UUID}\.db$")
    km = pattern.match(key)
    if bucket != BUCKET or not km:
        raise DatasetUnavailable("Dataset reference does not belong to this deployment.")
    if user_id is not None and km.group("uid") != user_id:
        raise DatasetUnavailable("Dataset does not belong to this user.")
    return bucket, key


# Workspace-owned objects (Sprint 3): <prefix>/ws/<workspace_id>/<dataset_id>/curated-<run_id>.db and .../raw/<run_id>.<ext>.
# Access is decided by the catalog (workspaces.session_db_path), never by this layout; the layout is only re-validated here so a
# tampered reference can never reach another object. The workspace in the path is the one the dataset was CREATED in.
_WS_REL = re.compile(rf"^ws/(?P<ws>{_UUID})/(?P<ds>{_UUID})/(?P<name>(?:raw/)?[A-Za-z0-9._-]{{1,80}})$")


def _parse_ws(ref: Optional[str]):
    """(workspace_id, dataset_id, name, relative_key) for a valid workspace object reference of THIS deployment, else None."""
    if not ref:
        return None
    if is_remote(ref):
        m = _S3_REF.match(ref)
        if not m or m.group("bucket") != BUCKET or not m.group("key").startswith(PREFIX + "/"):
            return None
        rel = m.group("key")[len(PREFIX) + 1:]
    else:
        try:
            root, real = os.path.realpath(UPLOAD_DIR), os.path.realpath(ref)
            if os.path.commonpath([root, real]) != root:
                return None
        except ValueError:
            return None
        rel = os.path.relpath(real, root).replace(os.sep, "/")
    m = _WS_REL.match(rel)
    return (m.group("ws"), m.group("ds"), m.group("name"), rel) if m else None


def is_workspace_ref(ref: Optional[str]) -> bool:
    return _parse_ws(ref) is not None


def store_workspace_object(local_path: str, workspace_id: str, dataset_id: str, name: str, *, cache: bool = False) -> str:
    """Move ``local_path`` into workspace storage and return its reference. s3: uploaded (``cache=True`` keeps the local file as
    the read cache, for a curated database about to be opened); local: moved under uploads/ws/. Raises StorageError."""
    rel = f"ws/{workspace_id}/{dataset_id}/{name}"
    if BACKEND != "s3":
        dest = os.path.join(UPLOAD_DIR, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(local_path, dest)
        return dest
    key = f"{PREFIX}/{rel}"
    try:
        _client().upload_file(local_path, BUCKET, key)
    except _client_error_types() as e:
        logger.error("workspace object upload to s3 failed: %s", e)
        raise StorageError(str(e)) from e
    if cache:
        os.makedirs(CACHE_DIR, exist_ok=True)
        os.replace(local_path, _cache_path(key))
        _evict_cache(protect=_cache_path(key))
    return f"s3://{BUCKET}/{key}"


def delete_workspace_object(ref: Optional[str]) -> bool:
    """Best-effort removal of one workspace object (an old curated version, an expired raw file). Never raises."""
    parsed = _parse_ws(ref)
    if not parsed:
        return False
    try:
        if is_remote(ref):
            key = f"{PREFIX}/{parsed[3]}"
            if BACKEND == "s3":
                _client().delete_object(Bucket=BUCKET, Key=key)
            cp = _cache_path(key)
            if os.path.exists(cp):
                os.remove(cp)
        elif os.path.exists(ref):
            os.remove(ref)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("could not delete workspace object: %s", type(e).__name__)
        return False


def delete_workspace_tree(ref: str) -> None:
    """STRICT removal of everything a dataset owns (every curated version and raw file). Raises on failure."""
    parsed = _parse_ws(ref)
    if not parsed:
        raise DatasetUnavailable("Not a workspace dataset reference.")
    prefix = f"ws/{parsed[0]}/{parsed[1]}/"
    if not is_remote(ref):
        tree = os.path.join(UPLOAD_DIR, *prefix.split("/"))
        if os.path.isdir(tree):
            shutil.rmtree(tree)
        for parent in (os.path.dirname(tree.rstrip(os.sep)), os.path.join(UPLOAD_DIR, "ws")):   # tidy now-empty folders
            try:
                os.rmdir(parent)
            except OSError:
                pass
        return
    if BACKEND != "s3":
        raise StorageError("Dataset references object storage, but the s3 backend is not enabled.")
    try:
        client = _client()
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=f"{PREFIX}/{prefix}"):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                client.delete_objects(Bucket=BUCKET, Delete={"Objects": keys})
            for k in keys:
                cp = _cache_path(k["Key"])
                if os.path.exists(cp):
                    os.remove(cp)
    except _client_error_types() as e:
        raise StorageError(str(e)) from e


def _cache_path(key: str) -> str:
    # key was validated by _parse_remote (fixed layout, no traversal chars).
    return os.path.join(CACHE_DIR, key.replace("/", "__"))


def _inside_upload_dir(path: str) -> bool:
    try:
        real_upload = os.path.realpath(UPLOAD_DIR)
        real_path = os.path.realpath(path)
        return os.path.commonpath([real_upload, real_path]) == real_upload and real_path != real_upload
    except ValueError:
        return False


def is_managed(ref: Optional[str]) -> bool:
    """True if `ref` is a dataset *we* created (an upload), as opposed to the
    bundled sample database or anything else. Only managed datasets may ever
    be deleted."""
    if not ref or is_workspace_ref(ref):
        return False  # workspace datasets are deleted only through the catalog (workspaces.delete_dataset)
    if is_remote(ref):
        try:
            _parse_remote(ref)
            return True
        except DatasetUnavailable:
            return False
    return _inside_upload_dir(ref)


def _evict_cache(protect: Optional[str] = None) -> None:
    """Keeps the local download cache under CACHE_MAX_BYTES by removing the
    least-recently-used files first."""
    try:
        entries = []
        for name in os.listdir(CACHE_DIR):
            if name.startswith(".tmp-"):
                continue
            fp = os.path.join(CACHE_DIR, name)
            if os.path.isfile(fp):
                st = os.stat(fp)
                entries.append((st.st_mtime, st.st_size, fp))
        total = sum(sz for _, sz, _ in entries)
        for _, sz, fp in sorted(entries):
            if total <= CACHE_MAX_BYTES:
                break
            if protect and os.path.abspath(fp) == os.path.abspath(protect):
                continue
            try:
                os.remove(fp)
                total -= sz
            except OSError:
                pass
    except FileNotFoundError:
        pass



_STAGED_REF = re.compile(r"^s3://(?P<bucket>[^/]+)/(?P<key>staging/(?P<uid>[^/]+)/(?P<name>[0-9a-fA-F-]{36}(?:\.[A-Za-z0-9]+)?))$")

def _parse_staged(ref: str, user_id: Optional[str] = None) -> Tuple[str, str]:
    m = _STAGED_REF.match(ref or "")
    if not m or m.group("bucket") != BUCKET:
        raise DatasetUnavailable("Malformed staged upload reference.")
    if user_id is not None and m.group("uid") != user_id:
        raise DatasetUnavailable("Staged upload does not belong to this user.")
    return m.group("bucket"), m.group("key")

def stage_upload(local_path: str, user_id: str, extension: str) -> str:
    """Durably stage an uploaded source object for a worker. Production uses S3;
    local development deliberately returns the local path for the inline fallback.
    """
    if BACKEND != "s3":
        return local_path
    key = f"staging/{user_id}/{uuid.uuid4()}{extension.lower()}"
    try:
        _client().upload_file(local_path, BUCKET, key)
    except _client_error_types() as e:
        logger.error("upload staging to s3 failed: %s", e)
        raise StorageError(str(e)) from e
    return f"s3://{BUCKET}/{key}"

def ensure_staged_local(ref: str, user_id: str, suffix: str) -> str:
    if not is_remote(ref):
        if ref and os.path.exists(ref):
            return ref
        raise DatasetUnavailable("The staged upload file no longer exists.")
    if BACKEND != "s3":
        raise StorageError("Staged upload references object storage, but the s3 backend is not enabled.")
    _, key = _parse_staged(ref, user_id=user_id)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    tmp = os.path.join(UPLOAD_DIR, f".staged-{uuid.uuid4().hex}{suffix}")
    try:
        _client().download_file(BUCKET, key, tmp)
    except _client_error_types() as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        logger.error("staged upload download failed: %s", e)
        raise StorageError(str(e)) from e
    return tmp

def delete_staged(ref: Optional[str], user_id: Optional[str] = None) -> bool:
    if not ref or not is_remote(ref):
        return False
    try:
        _, key = _parse_staged(ref, user_id=user_id)
        if BACKEND == "s3":
            _client().delete_object(Bucket=BUCKET, Key=key)
            return True
    except Exception as e:
        logger.warning("could not delete staged upload %s: %s", ref, e)
    return False

# ------------------------------------------------------------------ public API

def store_dataset(local_path: str, user_id: str) -> str:
    """Makes `local_path` (a finished SQLite file) durable and returns the
    reference to persist as the session's `db_path`.

    local backend: returns `local_path` unchanged.
    s3 backend:    uploads it, moves the local file into the cache, returns
                   `s3://bucket/key`. Raises StorageError on failure, leaving
                   `local_path` in place for the caller to clean up.
    """
    if BACKEND != "s3":
        return local_path

    key = f"{PREFIX}/{user_id}/{uuid.uuid4()}.db"
    try:
        _client().upload_file(local_path, BUCKET, key)
    except _client_error_types() as e:
        logger.error("dataset upload to s3 failed: %s", e)
        raise StorageError(str(e)) from e

    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = _cache_path(key)
    try:
        os.replace(local_path, cache_path)
    except OSError:
        shutil.move(local_path, cache_path)
    _evict_cache(protect=cache_path)
    return f"s3://{BUCKET}/{key}"


def ensure_local(ref: str, user_id: Optional[str] = None) -> str:
    """Legacy (user-owned) datasets only. A workspace reference is refused here: those are opened through the catalog
    (``ensure_local_workspace``) after a membership check, so a tampered session row can never read one."""
    if is_workspace_ref(ref):
        raise DatasetUnavailable("Dataset does not belong to this user.")
    return _ensure_local(ref, user_id)


def ensure_local_workspace(ref: str) -> str:
    """Local path of a workspace-owned object. The CALLER must already have authorized access through the catalog."""
    if not is_workspace_ref(ref):
        raise DatasetUnavailable("Not a workspace dataset reference.")
    return _ensure_local(ref, None)


def _ensure_local(ref: str, user_id: Optional[str] = None) -> str:
    """Returns a local filesystem path the query engine can open for `ref`,
    downloading it from object storage if it isn't cached (first use after a
    restart/redeploy).

    Raises DatasetUnavailable if the dataset is gone, StorageError if the
    backend itself is failing.
    """
    if not is_remote(ref):
        if ref and os.path.exists(ref):
            return ref
        raise DatasetUnavailable("The stored dataset file no longer exists.")

    if BACKEND != "s3":
        raise StorageError("Session references object storage, but the s3 backend is not enabled.")

    ws = _parse_ws(ref)
    key = f"{PREFIX}/{ws[3]}" if ws else _parse_remote(ref, user_id=user_id)[1]
    cache_path = _cache_path(key)
    if os.path.exists(cache_path):
        try:
            os.utime(cache_path)  # mark as recently used for LRU eviction
        except OSError:
            pass
        return cache_path

    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp_path = os.path.join(CACHE_DIR, f".tmp-{uuid.uuid4().hex}")
    try:
        _client().download_file(BUCKET, key, tmp_path)
    except _client_error_types() as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound"):
            raise DatasetUnavailable("The stored dataset no longer exists.") from e
        logger.error("dataset download from s3 failed: %s", e)
        raise StorageError(str(e)) from e

    os.replace(tmp_path, cache_path)  # atomic: concurrent requests never see a partial file
    _evict_cache(protect=cache_path)
    return cache_path


def delete_dataset(ref: Optional[str]) -> bool:
    """Best-effort removal of a managed dataset (object + any cached copy).
    Never raises; never touches anything that isn't a managed upload (e.g. the
    bundled sample database). Returns True if something was removed."""
    if not ref or not is_managed(ref):
        return False
    try:
        if is_remote(ref):
            _, key = _parse_remote(ref)
            removed = False
            if BACKEND == "s3":
                _client().delete_object(Bucket=BUCKET, Key=key)
                removed = True
            cp = _cache_path(key)
            if os.path.exists(cp):
                os.remove(cp)
                removed = True
            return removed
        if os.path.exists(ref):
            os.remove(ref)
            return True
    except Exception as e:  # cleanup must never break a request
        logger.warning("could not delete dataset %s: %s", ref, e)
    return False


def discard_local(path: Optional[str]) -> None:
    """Removes a temporary local upload file (failed/rejected upload)."""
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


_probe_client = None


def health_check() -> bool:
    """Sprint 4 readiness probe: can we reach the object store? Local backend: the upload dir is writable. S3 backend:
    ``HEAD bucket`` with the app's credentials/endpoint but a dedicated client with tight timeouts and no retries, so a
    black-holed endpoint cannot hang a readiness probe for the SDK's default minute. Never raises."""
    global _probe_client
    try:
        if BACKEND != "s3":
            return os.path.isdir(UPLOAD_DIR) and os.access(UPLOAD_DIR, os.W_OK)
        if _probe_client is None:
            import boto3
            from botocore.config import Config
            kwargs = {"config": Config(connect_timeout=2, read_timeout=3, retries={"max_attempts": 1})}
            if REGION:
                kwargs["region_name"] = REGION
            if ENDPOINT_URL:
                kwargs["endpoint_url"] = ENDPOINT_URL
            _probe_client = boto3.client("s3", **kwargs)
        _probe_client.head_bucket(Bucket=BUCKET)
        return True
    except Exception:  # noqa: BLE001
        return False
