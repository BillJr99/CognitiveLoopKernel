"""Docker-host fallback for localhost provider endpoints.

When CLK runs inside a container, ``http://localhost:PORT`` cannot
reach an ollama / OpenWebUI server bound to the host's loopback. On
Docker Desktop (and on Linux with
``--add-host=host.docker.internal:host-gateway``) the host is reachable
at ``host.docker.internal`` instead.

This module centralises the "probe localhost, fall back to
host.docker.internal" logic shared by every HTTP-based provider so
runtime ``available()`` checks recover automatically without having to
re-run setup.
"""

from __future__ import annotations

import socket
import sys
from typing import Optional
from urllib.parse import urlparse, urlunparse

from ..log import get_logger

logger = get_logger(__name__)

_LOCALHOST_HOSTS = {"localhost", "127.0.0.1", "::1"}
_DOCKER_HOST = "host.docker.internal"


def normalize_endpoint(endpoint: str) -> str:
    """Ensure ``endpoint`` has a scheme.

    Users often type ``host.docker.internal:11434`` without ``http://``;
    ``urlparse`` then misreads the host:port as ``scheme:path`` and the
    probe targets the wrong place. Prepend ``http://`` when no scheme is
    present so the rest of the pipeline resolves host/port correctly.
    """
    ep = (endpoint or "").strip()
    if not ep:
        return ep
    if "://" not in ep:
        ep = "http://" + ep
    return ep


def _port_for(url) -> int:
    if url.port:
        return url.port
    return 443 if url.scheme == "https" else 80


def probe_endpoint(endpoint: str, timeout: float = 1.0) -> bool:
    """Return True if a TCP connection to ``endpoint`` succeeds quickly."""
    try:
        url = urlparse(endpoint)
        host = url.hostname or "localhost"
        with socket.create_connection((host, _port_for(url)), timeout=timeout):
            return True
    except Exception:
        return False


def docker_host_swap(endpoint: str) -> Optional[str]:
    """Return the host.docker.internal version of ``endpoint``, or None.

    Returns None when the original endpoint does not target localhost.
    """
    try:
        url = urlparse(endpoint)
    except Exception as _exc:
        logger.debug("could not parse endpoint %r: %s", endpoint, _exc)
        return None
    if (url.hostname or "").lower() not in _LOCALHOST_HOSTS:
        return None
    netloc = _DOCKER_HOST
    if url.port:
        netloc = f"{_DOCKER_HOST}:{url.port}"
    if url.username or url.password:
        creds = url.username or ""
        if url.password:
            creds = f"{creds}:{url.password}"
        netloc = f"{creds}@{netloc}"
    return urlunparse(url._replace(netloc=netloc))


def maybe_docker_host_fallback(
    endpoint: str,
    *,
    label: str = "endpoint",
    timeout: float = 1.0,
) -> Optional[str]:
    """If ``endpoint`` (localhost) is dead but host.docker.internal works,
    return the swapped URL and log a one-line notice to stderr. Otherwise
    return None.

    The notice is emitted exactly once per (label, swap) pair so noisy
    health-check loops don't spam the log.
    """
    if probe_endpoint(endpoint, timeout=timeout):
        return None
    candidate = docker_host_swap(endpoint)
    if not candidate or candidate == endpoint:
        return None
    if not probe_endpoint(candidate, timeout=timeout):
        return None
    key = (label, endpoint, candidate)
    if key not in _NOTIFIED:
        _NOTIFIED.add(key)
        print(
            f"[{label}] {endpoint} unreachable from this container; "
            f"auto-switching to {candidate} (host.docker.internal).",
            file=sys.stderr,
        )
    return candidate


_NOTIFIED: set = set()
