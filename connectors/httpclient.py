"""One bounded, pinned HTTP GET for REST sources (Sprint 5).

The connection goes to an address that netguard just validated (not a second DNS lookup), TLS is verified against the host name
(SNI + certificate), redirects are never followed, the body is size-capped, and only JSON is accepted. Errors are codes, never
response text. Standard library only.
"""
from __future__ import annotations

import http.client
import json
import socket
import ssl
from typing import Optional, Tuple
from urllib.parse import urlsplit

import config
from connectors import netguard
from connectors.database import ConnectorError


def get_json(url: str, headers: dict, *, max_bytes: Optional[int] = None) -> Tuple[object, dict]:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConnectorError("INVALID_URL", "The request URL is not valid.")
    if parts.scheme == "http" and config.IS_PRODUCTION:
        raise ConnectorError("TLS_REQUIRED", "Plain HTTP is not allowed in production; use https.")
    host, port = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
    limit = max_bytes or config.SOURCE_MAX_RESPONSE_MB * 1024 * 1024
    try:
        ips = netguard.resolve_allowed(host, port)
    except netguard.HostNotAllowed as exc:
        raise ConnectorError("HOST_NOT_ALLOWED", str(exc)) from None
    except ConnectionError as exc:
        raise ConnectorError("HOST_UNRESOLVED", str(exc), transient=True) from None
    conn = None
    try:
        sock = socket.create_connection((ips[0], port), timeout=config.CONNECTION_TIMEOUT_SECONDS)
        sock.settimeout(config.CONNECTION_QUERY_TIMEOUT_SECONDS)
        if parts.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            conn = http.client.HTTPSConnection(host, port, timeout=config.CONNECTION_QUERY_TIMEOUT_SECONDS)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=config.CONNECTION_QUERY_TIMEOUT_SECONDS)
        conn.sock = sock                       # already connected to the validated address
        path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
        conn.request("GET", path, headers={"User-Agent": "QuerySure", "Accept": "application/json", **headers})
        resp = conn.getresponse()
        status = resp.status
        meta = {"retry_after": resp.getheader("Retry-After")}
        if 300 <= status < 400:
            raise ConnectorError("REDIRECT_REFUSED", "The server answered with a redirect, which is not followed.")
        body = resp.read(limit + 1)
    except ConnectorError:
        raise
    except ssl.SSLError:
        raise ConnectorError("TLS_FAILED", "A secure connection could not be established.") from None
    except (socket.timeout, TimeoutError):
        raise ConnectorError("QUERY_TIMEOUT", "The server took too long to answer.", transient=True) from None
    except (OSError, http.client.HTTPException):
        raise ConnectorError("CONNECT_FAILED", "The server could not be reached.", transient=True) from None
    finally:
        if conn is not None:
            conn.close()
    if status in (401, 403):
        raise ConnectorError("AUTH_FAILED", "The server rejected the credentials.")
    if status == 404:
        raise ConnectorError("NOT_FOUND", "The endpoint was not found.")
    if status == 429 or status >= 500:
        err = ConnectorError("RATE_LIMITED" if status == 429 else "SERVER_ERROR", "The server is busy or failing; try again later.", transient=True)
        err.retry_after = meta["retry_after"]
        raise err
    if status >= 400:
        raise ConnectorError("REQUEST_REJECTED", "The server rejected the request.")
    if len(body) > limit:
        raise ConnectorError("RESPONSE_TOO_LARGE", f"A response was larger than {limit // (1024 * 1024)} MB.")
    try:
        return json.loads(body.decode("utf-8")), meta
    except (ValueError, UnicodeDecodeError):
        raise ConnectorError("INVALID_RESPONSE", "The server did not answer with JSON.") from None
