"""Configurable REST API source (Sprint 5).

A connection holds a base URL, an auth method and one or more *resources*; each resource names an endpoint path, where the
records are in the JSON answer (``records_path``), how it is paged, which fields identify a record (``primary_key``) and,
optionally, an ``updated_since`` parameter that makes incremental sync possible. Everything is GET-only, bounded (pages, bytes,
time) and goes through connectors/httpclient.py (validated address, verified TLS, no redirects). Secrets (a token or password)
are supplied separately and encrypted; they are only ever placed in a request header.
"""
from __future__ import annotations

import base64
import re
import time
from typing import Iterator, List, Optional
from urllib.parse import urlencode, urlsplit

import config
from connectors import httpclient, netguard
from connectors.base import Capabilities, ConnectionResult
from connectors.database import ConnectorError, SourceTable
from connectors.records import NAME_RE, flatten, get_path, infer_columns

MAX_RESOURCES = 20
RETRIES = 3
_PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/\-]*$")
_DOTTED = re.compile(r"^[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+)*$")
_FIELD = re.compile(r"^[A-Za-z0-9_\-.]{1,128}$")
_PARAM = re.compile(r"^[A-Za-z0-9_.\-\[\]]{1,64}$")
_HEADER = re.compile(r"^[A-Za-z0-9\-]{1,64}$")
_FORBIDDEN_HEADERS = {"authorization", "cookie", "proxy-authorization", "host", "content-length", "transfer-encoding", "connection"}
_PAGINATION = {"none", "page", "offset", "cursor", "next_url"}


class RestConnector:
    name, label = "rest", "REST API"
    pipeline = "records"
    credential_keys = ("base_url", "auth")

    def capabilities(self) -> Capabilities:
        return Capabilities(full_refresh=True, incremental=True, extensions=(), credentials=True)

    # ------------------------------------------------------------------ configuration
    def validate_config(self, cfg: Optional[dict]) -> dict:
        cfg = dict(cfg or {})
        unknown = set(cfg) - {"base_url", "auth", "headers", "resources"}
        if unknown:
            raise ValueError(f"Unknown connection setting(s): {', '.join(sorted(unknown))}.")
        base = self._base_url(cfg.get("base_url"))
        auth = self._auth(cfg.get("auth") or {"type": "none"})
        headers = self._headers(cfg.get("headers") or {})
        resources = cfg.get("resources")
        if not isinstance(resources, list) or not 1 <= len(resources) <= MAX_RESOURCES:
            raise ValueError(f"Define between 1 and {MAX_RESOURCES} resources.")
        out, seen = [], set()
        for r in resources:
            res = self._resource(r)
            if res["name"] in seen:
                raise ValueError(f"Resource name '{res['name']}' is used twice.")
            seen.add(res["name"])
            out.append(res)
        return {"base_url": base, "auth": auth, "headers": headers, "resources": out}

    @staticmethod
    def _base_url(value) -> str:
        parts = urlsplit(value if isinstance(value, str) else "")
        allowed = ("https",) if config.IS_PRODUCTION else ("https", "http")
        if parts.scheme not in allowed or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("base_url must be an https URL without credentials, query or fragment" +
                             ("" if config.IS_PRODUCTION else " (http is allowed in development)") + ".")
        netguard.validate_host(parts.hostname)
        if parts.port is not None:
            netguard.validate_port(parts.port)
        return value.rstrip("/")

    @staticmethod
    def _auth(auth: dict) -> dict:
        t = auth.get("type", "none") if isinstance(auth, dict) else None
        if t not in ("none", "bearer", "header", "basic"):
            raise ValueError("auth.type must be none, bearer, header or basic.")
        out = {"type": t}
        if t == "header":
            if not _HEADER.match(str(auth.get("header", ""))) or str(auth["header"]).lower() in _FORBIDDEN_HEADERS:
                raise ValueError("auth.header must be a valid header name (not Authorization, Cookie or Host).")
            out["header"] = auth["header"]
        if t == "basic":
            user = auth.get("username")
            if not isinstance(user, str) or not 0 < len(user) <= 128 or ":" in user or any(ord(c) < 32 for c in user):
                raise ValueError("auth.username is required for basic auth (no colon).")
            out["username"] = user
        return out

    @staticmethod
    def _headers(headers: dict) -> dict:
        if not isinstance(headers, dict) or len(headers) > 10:
            raise ValueError("headers must be an object with at most 10 entries.")
        for k, v in headers.items():
            if not _HEADER.match(str(k)) or str(k).lower() in _FORBIDDEN_HEADERS:
                raise ValueError(f"Header '{k}' is not allowed here; credentials belong in the saved password, not in headers.")
            if not isinstance(v, str) or len(v) > 512 or any(ord(c) < 32 for c in v):
                raise ValueError(f"Header '{k}' must be a short single-line string.")
        return dict(headers)

    @staticmethod
    def _resource(r) -> dict:
        if not isinstance(r, dict):
            raise ValueError("Each resource must be an object.")
        unknown = set(r) - {"name", "path", "query", "records_path", "pagination", "primary_key", "updated_since"}
        if unknown:
            raise ValueError(f"Unknown resource setting(s): {', '.join(sorted(unknown))}.")
        if not NAME_RE.match(str(r.get("name", ""))):
            raise ValueError("Resource name must start with a letter and use letters, digits and underscores (max 63).")
        path = r.get("path", "")
        if not isinstance(path, str) or not _PATH_RE.match(path) or ".." in path or "//" in path or len(path) > 512:
            raise ValueError(f"Resource '{r['name']}': path must start with / and be a plain URL path.")
        query = r.get("query") or {}
        if not isinstance(query, dict) or len(query) > 20 or any(
                not _PARAM.match(str(k)) or not isinstance(v, (str, int, float, bool)) or len(str(v)) > 256 for k, v in query.items()):
            raise ValueError(f"Resource '{r['name']}': query must be an object of simple values.")
        rp = r.get("records_path", "")
        if rp and (not isinstance(rp, str) or not _DOTTED.match(rp)):
            raise ValueError(f"Resource '{r['name']}': records_path must look like data.items.")
        pg = dict(r.get("pagination") or {"type": "none"})
        if pg.get("type") not in _PAGINATION:
            raise ValueError(f"Resource '{r['name']}': pagination.type must be one of {', '.join(sorted(_PAGINATION))}.")
        for key in ("param", "size_param"):
            if pg.get(key) is not None and not _PARAM.match(str(pg[key])):
                raise ValueError(f"Resource '{r['name']}': pagination.{key} is not a valid parameter name.")
        if pg["type"] in ("cursor", "next_url") and not _DOTTED.match(str(pg.get("next_path", ""))):
            raise ValueError(f"Resource '{r['name']}': pagination.next_path (where the next cursor/URL is) is required.")
        if pg["type"] in ("page", "offset", "cursor") and not pg.get("param"):
            raise ValueError(f"Resource '{r['name']}': pagination.param is required.")
        size = pg.get("size", 100)
        if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= 10000:
            raise ValueError(f"Resource '{r['name']}': pagination.size must be between 1 and 10000.")
        pg["size"] = size
        pk = r.get("primary_key") or []
        if not isinstance(pk, list) or len(pk) > 5 or any(not _FIELD.match(str(k)) for k in pk):
            raise ValueError(f"Resource '{r['name']}': primary_key must be a list of up to 5 field names.")
        us = r.get("updated_since")
        if us is not None:
            if (not isinstance(us, dict) or not _PARAM.match(str(us.get("param", ""))) or not _FIELD.match(str(us.get("field", "")))
                    or us.get("format", "iso") not in ("iso", "epoch")):
                raise ValueError(f"Resource '{r['name']}': updated_since needs param, field and format (iso or epoch).")
            us = {"param": us["param"], "field": us["field"], "format": us.get("format", "iso")}
        return {"name": r["name"], "path": path, "query": query, "records_path": rp, "pagination": pg,
                "primary_key": [str(k) for k in pk], "updated_since": us}

    def summarize(self, cfg: dict) -> str:
        return f"{cfg.get('base_url')} · {len(cfg.get('resources') or [])} resource(s)"

    # ------------------------------------------------------------------ requests
    def _auth_headers(self, cfg: dict, secrets: dict) -> dict:
        secret = (secrets or {}).get("password") or ""
        auth, h = cfg.get("auth") or {"type": "none"}, dict(cfg.get("headers") or {})
        if auth["type"] != "none" and not secret:
            raise ConnectorError("AUTH_REQUIRED", "This connection needs its token or password.")
        if auth["type"] == "bearer":
            h["Authorization"] = f"Bearer {secret}"
        elif auth["type"] == "header":
            h[auth["header"]] = secret
        elif auth["type"] == "basic":
            h["Authorization"] = "Basic " + base64.b64encode(f"{auth['username']}:{secret}".encode()).decode()
        return h

    def _get(self, cfg: dict, secrets: dict, url: str):
        headers = self._auth_headers(cfg, secrets)
        for attempt in range(RETRIES):
            try:
                data, _ = httpclient.get_json(url, headers)
                return data
            except ConnectorError as exc:
                if not exc.transient or attempt == RETRIES - 1:
                    raise
                try:
                    wait = float(getattr(exc, "retry_after", None) or 0)
                except (TypeError, ValueError):
                    wait = 0
                time.sleep(min(wait or 2 ** attempt, 30))

    def resource(self, cfg: dict, name: str) -> Optional[dict]:
        return next((r for r in cfg.get("resources", []) if r["name"] == name), None)

    @staticmethod
    def _same_origin(base: str, url: str) -> str:
        b, u = urlsplit(base), urlsplit(url)
        if not u.scheme and not u.netloc:                      # relative: resolve against the base origin
            return f"{b.scheme}://{b.netloc}{url if url.startswith('/') else '/' + url}"
        if (u.scheme, u.netloc) != (b.scheme, b.netloc):
            raise ConnectorError("NEXT_URL_REFUSED", "The next-page URL points to a different server and was not followed.")
        return url

    def pages(self, cfg: dict, secrets: dict, resource: dict, since=None) -> Iterator[List[dict]]:
        """Pages of records. ``since`` (a datetime or int, from the resource's watermark) is sent as its updated_since parameter."""
        pg, us = resource["pagination"], resource.get("updated_since")
        size, kind = pg["size"], pg["type"]
        page_no, offset, cursor, next_url = pg.get("start", 1), 0, None, None
        for _ in range(config.SOURCE_MAX_PAGES):
            if next_url:
                url = next_url
            else:
                params = dict(resource.get("query") or {})
                if since is not None and us:
                    params[us["param"]] = (int(since) if us["format"] == "epoch" else since.strftime("%Y-%m-%dT%H:%M:%SZ"))
                if kind == "page":
                    params[pg["param"]] = page_no
                elif kind == "offset":
                    params[pg["param"]] = offset
                elif kind == "cursor" and cursor:
                    params[pg["param"]] = cursor
                if pg.get("size_param") and kind != "none":
                    params[pg["size_param"]] = size
                url = cfg["base_url"] + resource["path"] + (("?" + urlencode(params)) if params else "")
            data = self._get(cfg, secrets, url)
            records = get_path(data, resource["records_path"]) if resource["records_path"] else data
            if not isinstance(records, list):
                raise ConnectorError("INVALID_RESPONSE", "The records were not found at the configured records_path.")
            records = [r for r in records if isinstance(r, dict)]
            if not records:
                return
            yield records
            if kind == "page":
                if len(records) < size:
                    return
                page_no += 1
            elif kind == "offset":
                if len(records) < size:
                    return
                offset += len(records)
            elif kind == "cursor":
                cursor = get_path(data, pg["next_path"])
                if not cursor:
                    return
            elif kind == "next_url":
                nxt = get_path(data, pg["next_path"])
                if not nxt:
                    return
                next_url = self._same_origin(cfg["base_url"], str(nxt))
            else:
                return
        raise ConnectorError("TOO_MANY_PAGES", f"More than {config.SOURCE_MAX_PAGES} pages; narrow the resource or raise SOURCE_MAX_PAGES.")

    # ------------------------------------------------------------------ interface used by the connection API
    def discover_schema(self, cfg: dict, secrets: dict) -> List[SourceTable]:
        tables = []
        for res in cfg["resources"]:
            first = next(self.pages(cfg, secrets, res), [])
            us = res.get("updated_since")
            cols = infer_columns(first, {us["field"]} if us else None)
            names = {c.name for c in cols}
            wm = us["field"] if us else None
            ready = bool(res["primary_key"]) and all(k in names for k in res["primary_key"]) and bool(wm) and wm in names
            tables.append(SourceTable(name=res["name"], columns=cols, primary_key=list(res["primary_key"]),
                                      incremental_ready=ready, watermark=wm if ready else None))
        return tables

    def test_connection(self, cfg: dict, secrets: dict) -> ConnectionResult:
        res = cfg["resources"][0]
        first = next(self.pages(cfg, secrets, res), [])
        return ConnectionResult(True, f"Connected ({len(first)} sample record(s) from '{res['name']}').")


__all__ = ["RestConnector", "flatten"]
