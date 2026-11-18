"""Amazon S3 / S3-compatible object source (Sprint 5).

A *resource* is a prefix plus a file format; the dataset is every matching object concatenated into one table, each row tagged with
its ``source_object`` and ``source_modified``. Incremental sync lists the prefix and compares each object's ETag and LastModified
with the previous run, so only new or changed objects are downloaded and objects that disappeared are removed.

Security: credentials are always explicit (an access key id in the connection, the secret key encrypted). The default AWS
credential chain, instance roles and environment variables are never used, so a connection cannot borrow the server's own
identity. A custom ``endpoint_url`` is checked by the same network guard as database hosts.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from datetime import timezone
from typing import List, Optional
from urllib.parse import urlsplit

import config
from connectors import netguard
from connectors.base import Capabilities, ConnectionResult
from connectors.database import ConnectorError, SourceColumn, SourceTable
from connectors.records import NAME_RE

FORMATS = {"csv": ("csv", (".csv",)), "json": ("json", (".json",)), "parquet": ("parquet", (".parquet",)),
           "excel": ("excel", (".xlsx", ".xlsm"))}
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")
_REGION = re.compile(r"^[a-z0-9\-]{2,32}$")
_KEY_ID = re.compile(r"^[A-Za-z0-9]{8,128}$")
SOURCE_COLUMNS = ("source_object", "source_modified")


class S3SourceConnector:
    name, label = "s3", "Amazon S3 / S3-compatible"
    pipeline = "objects"
    credential_keys = ("endpoint_url", "access_key_id", "region")

    def capabilities(self) -> Capabilities:
        return Capabilities(full_refresh=True, incremental=True, extensions=(), credentials=True)

    def validate_config(self, cfg: Optional[dict]) -> dict:
        cfg = dict(cfg or {})
        unknown = set(cfg) - {"bucket", "region", "endpoint_url", "access_key_id", "resources"}
        if unknown:
            raise ValueError(f"Unknown connection setting(s): {', '.join(sorted(unknown))}.")
        if not _BUCKET.match(str(cfg.get("bucket", ""))):
            raise ValueError("bucket must be a valid S3 bucket name.")
        if not _KEY_ID.match(str(cfg.get("access_key_id", ""))):
            raise ValueError("access_key_id is required (the secret access key goes in the password field).")
        out = {"bucket": cfg["bucket"], "access_key_id": cfg["access_key_id"]}
        if cfg.get("region"):
            if not _REGION.match(str(cfg["region"])):
                raise ValueError("region is not valid.")
            out["region"] = cfg["region"]
        if cfg.get("endpoint_url"):
            parts = urlsplit(str(cfg["endpoint_url"]))
            allowed = ("https",) if config.IS_PRODUCTION else ("https", "http")
            if parts.scheme not in allowed or not parts.hostname or parts.username or parts.password or parts.path.strip("/") or parts.query:
                raise ValueError("endpoint_url must be an https URL of the S3-compatible service (no path or credentials).")
            netguard.validate_host(parts.hostname)
            out["endpoint_url"] = str(cfg["endpoint_url"]).rstrip("/")
        resources = cfg.get("resources")
        if not isinstance(resources, list) or not 1 <= len(resources) <= 20:
            raise ValueError("Define between 1 and 20 resources (a prefix and a format each).")
        seen, norm = set(), []
        for r in resources:
            if not isinstance(r, dict) or set(r) - {"name", "prefix", "format"}:
                raise ValueError("Each resource is an object with name, prefix and format.")
            if not NAME_RE.match(str(r.get("name", ""))) or r["name"] in seen:
                raise ValueError("Resource names must be unique and start with a letter (letters, digits, underscores).")
            prefix = r.get("prefix", "")
            if (not isinstance(prefix, str) or len(prefix) > 512 or prefix.startswith("/") or ".." in prefix
                    or any(ord(c) < 32 for c in prefix)):
                raise ValueError(f"Resource '{r['name']}': prefix must be a plain key prefix (no leading slash).")
            if r.get("format") not in FORMATS:
                raise ValueError(f"Resource '{r['name']}': format must be one of {', '.join(sorted(FORMATS))}.")
            seen.add(r["name"])
            norm.append({"name": r["name"], "prefix": prefix, "format": r["format"]})
        out["resources"] = norm
        return out

    def summarize(self, cfg: dict) -> str:
        key = cfg.get("access_key_id", "")
        return f"s3://{cfg.get('bucket')} · {len(cfg.get('resources') or [])} resource(s) · key …{key[-4:]}"

    def resource(self, cfg: dict, name: str) -> Optional[dict]:
        return next((r for r in cfg.get("resources", []) if r["name"] == name), None)

    # ------------------------------------------------------------------ client
    def _client(self, cfg: dict, secrets: dict):
        import boto3
        from botocore.config import Config
        secret = (secrets or {}).get("password")
        if not secret:
            raise ConnectorError("AUTH_REQUIRED", "This connection needs its secret access key.")
        endpoint = cfg.get("endpoint_url")
        if endpoint:
            parts = urlsplit(endpoint)
            try:
                netguard.resolve_allowed(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
            except netguard.HostNotAllowed as exc:
                raise ConnectorError("HOST_NOT_ALLOWED", str(exc)) from None
            except ConnectionError as exc:
                raise ConnectorError("HOST_UNRESOLVED", str(exc), transient=True) from None
        return boto3.client(
            "s3", aws_access_key_id=cfg["access_key_id"], aws_secret_access_key=secret, region_name=cfg.get("region"),
            endpoint_url=endpoint,
            config=Config(connect_timeout=config.CONNECTION_TIMEOUT_SECONDS, read_timeout=config.CONNECTION_QUERY_TIMEOUT_SECONDS,
                          retries={"max_attempts": 2, "mode": "standard"}, signature_version="s3v4",
                          s3={"addressing_style": "path" if endpoint else "auto"}))

    @staticmethod
    def classify(exc: BaseException) -> ConnectorError:
        from botocore.exceptions import BotoCoreError, ClientError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError
        if isinstance(exc, ConnectorError):
            return exc
        if isinstance(exc, ClientError):
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in ("InvalidAccessKeyId", "SignatureDoesNotMatch", "AuthFailure", "ExpiredToken", "InvalidToken"):
                return ConnectorError("AUTH_FAILED", "The server rejected the access key or secret.")
            if code in ("AccessDenied", "AllAccessDisabled", "403", "Forbidden"):
                return ConnectorError("PERMISSION_DENIED", "The access key is not allowed to read this bucket or prefix.")
            if code in ("NoSuchBucket",):
                return ConnectorError("BUCKET_NOT_FOUND", "The bucket does not exist.")
            if code in ("NoSuchKey", "404", "NotFound"):
                return ConnectorError("NOT_FOUND", "The object or bucket was not found.")
            if code in ("SlowDown", "RequestTimeout", "InternalError", "ServiceUnavailable", "503", "500"):
                return ConnectorError("SERVER_ERROR", "The storage service is busy or failing; try again later.", transient=True)
            return ConnectorError("QUERY_FAILED", "The storage service rejected the request.")
        if isinstance(exc, (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError, BotoCoreError, OSError)):
            return ConnectorError("CONNECT_FAILED", "The storage service could not be reached.", transient=True)
        return ConnectorError("QUERY_FAILED", "The storage service could not complete the request.")

    # ------------------------------------------------------------------ reading
    def list_objects(self, cfg: dict, secrets: dict, resource: dict) -> List[dict]:
        """Matching objects, sorted by key: {key, etag, modified (UTC text), size}. Raises if there are more than the cap."""
        exts = FORMATS[resource["format"]][1]
        out: List[dict] = []
        try:
            client = self._client(cfg, secrets)
            for page in client.get_paginator("list_objects_v2").paginate(Bucket=cfg["bucket"], Prefix=resource["prefix"]):
                for o in page.get("Contents", []):
                    key = o["Key"]
                    if key.endswith("/") or not key.lower().endswith(exts):
                        continue
                    out.append({"key": key, "etag": str(o.get("ETag", "")).strip('"'), "size": int(o.get("Size", 0)),
                                "modified": o["LastModified"].astimezone(timezone.utc).replace(tzinfo=None)
                                .isoformat(sep=" ")})
                    if len(out) > config.S3_SOURCE_MAX_OBJECTS:
                        raise ConnectorError("TOO_MANY_OBJECTS", f"More than {config.S3_SOURCE_MAX_OBJECTS} matching objects; narrow the prefix.")
        except Exception as exc:  # noqa: BLE001
            raise self.classify(exc) from None
        return sorted(out, key=lambda o: o["key"])

    def read_object(self, cfg: dict, secrets: dict, resource: dict, obj: dict):
        """One object as a normalized DataFrame (the same readers uploads use)."""
        from connectors import registry
        if obj["size"] > config.S3_SOURCE_MAX_OBJECT_MB * 1024 * 1024:
            raise ConnectorError("OBJECT_TOO_LARGE", f"An object is larger than {config.S3_SOURCE_MAX_OBJECT_MB} MB.")
        reader = registry.get(FORMATS[resource["format"]][0])
        tmp = tempfile.mkdtemp(prefix="qs-s3-")
        try:
            path = os.path.join(tmp, "object" + os.path.splitext(obj["key"])[1].lower())
            try:
                self._client(cfg, secrets).download_file(cfg["bucket"], obj["key"], path)
            except Exception as exc:  # noqa: BLE001
                raise self.classify(exc) from None
            from connectors.base import Rejects
            try:
                tables = reader.read_tables(path, "data", Rejects())
            except ValueError as exc:
                raise ConnectorError("INVALID_OBJECT", f"An object could not be read as {resource['format']}: {str(exc)[:120]}") from None
            if len(tables) != 1:
                raise ConnectorError("MULTIPLE_SHEETS", "Workbooks with several sheets are not supported for S3 sources.")
            return next(iter(tables.values()))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # ------------------------------------------------------------------ interface used by the connection API
    def discover_schema(self, cfg: dict, secrets: dict) -> List[SourceTable]:
        tables = []
        for res in cfg["resources"]:
            objs = self.list_objects(cfg, secrets, res)
            cols: List[SourceColumn] = []
            if objs:
                from connectors.database import describe_type
                df = self.read_object(cfg, secrets, res, objs[0])
                for name, dtype in df.dtypes.items():
                    t = str(dtype)
                    declared = "INTEGER" if t.startswith(("int", "uint", "bool")) else "REAL" if t.startswith("float") else "TEXT"
                    cols.append(SourceColumn(str(name), declared.lower(), declared, describe_type(declared.lower())[1]))
            cols += [SourceColumn("source_object", "text", "TEXT", "other"), SourceColumn("source_modified", "timestamp", "TIMESTAMP", "timestamp")]
            tables.append(SourceTable(name=res["name"], columns=cols, primary_key=[], incremental_ready=True, watermark="source_modified"))
        return tables

    def test_connection(self, cfg: dict, secrets: dict) -> ConnectionResult:
        try:
            self._client(cfg, secrets).head_bucket(Bucket=cfg["bucket"])
        except Exception as exc:  # noqa: BLE001
            raise self.classify(exc) from None
        n = len(self.list_objects(cfg, secrets, cfg["resources"][0]))
        return ConnectionResult(True, f"Connected ({n} matching object(s) under '{cfg['resources'][0]['name']}').")
