"""Outbound-connection guard for user-supplied database hosts (Sprint 4): a connector must not be usable to probe or reach the
server's own network. Every address a host name resolves to must be globally routable unless CONNECTOR_ALLOW_PRIVATE_HOSTS is on.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from typing import List

import config

_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$|^[0-9A-Fa-f:.]{2,45}$")


class HostNotAllowed(ValueError):
    code = "HOST_NOT_ALLOWED"


def validate_host(host: str) -> str:
    host = (host or "").strip()
    if not host or len(host) > 253 or not _HOST_RE.match(host):
        raise ValueError("Enter a host name or IP address (no scheme, path, port or spaces).")
    return host


def validate_port(port) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Port must be a number between 1 and 65535.")
    return port


def resolve_allowed(host: str, port: int) -> List[str]:
    """Resolve ``host`` and return its addresses, refusing the connection if any is non-public (unless allowed)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ConnectionError("The host name could not be resolved.") from exc
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        raise ConnectionError("The host name could not be resolved.")
    if not config.CONNECTOR_ALLOW_PRIVATE_HOSTS:
        for ip in ips:
            addr = ipaddress.ip_address(ip.split("%")[0])
            addr = addr.ipv4_mapped or addr if isinstance(addr, ipaddress.IPv6Address) else addr
            if not addr.is_global:
                raise HostNotAllowed("Connections to private or internal addresses are not allowed on this server.")
    return ips
