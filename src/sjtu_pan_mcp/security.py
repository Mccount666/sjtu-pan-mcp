"""Outbound-URL safety checks.

Every URL this server is about to request must pass :func:`assert_safe_url`
first. The rules:

  * scheme must be ``http`` or ``https`` (no ``file:``, ``ftp:``, ...)
  * the host must not be ``localhost`` (any spelling) and must not resolve
    to a loopback / private / link-local / reserved / multicast address
  * the host must match the allowlist (the SJTU pan site and its storage
    backend by default; extendable via the ``allowed_url_hosts`` config key)

Checks run on the literal host *and* on every address the host resolves
to, so a public-looking name pointing at an internal IP is rejected too.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import List, Tuple
from urllib.parse import urljoin, urlsplit

# Hosts this MCP is allowed to talk to. The pan site itself plus the
# Tencent COS backend the file API redirects to (the bundle references
# ``x-cos-security-token``).
DEFAULT_ALLOWED_HOSTS: Tuple[str, ...] = (
    "pan.sjtu.edu.cn",
    ".sjtu.edu.cn",
    ".myqcloud.com",
    ".qcloud.com",
    ".qcloudcos.com",
)

_LOCALHOST_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


class UnsafeUrlError(ValueError):
    """Raised when a URL fails the safety checks."""


def _allowed_hosts() -> Tuple[str, ...]:
    try:
        from .config import load_config

        extra = load_config().get("allowed_url_hosts")
    except Exception:  # config problems must not crash the check itself
        extra = None
    if isinstance(extra, list) and extra:
        return tuple(str(h).strip().lower() for h in extra if str(h).strip())
    return DEFAULT_ALLOWED_HOSTS


def _host_in_allowlist(host: str) -> bool:
    host = host.lower().rstrip(".")
    for pattern in _allowed_hosts():
        if pattern.startswith("."):
            if host.endswith(pattern) or host == pattern[1:]:
                return True
        elif host == pattern:
            return True
    return False


def _reject_ip(ip: ipaddress._BaseAddress, host: str) -> None:
    # IPv4-mapped/computed IPv6 forms are normalized by ipaddress already.
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise UnsafeUrlError(
            f"host {host!r} resolves to non-public address {ip}"
        )


def _check_host(host: str) -> None:
    if not host:
        raise UnsafeUrlError("URL has no host")
    if host.lower() in _LOCALHOST_NAMES:
        raise UnsafeUrlError(f"host {host!r} is a localhost name")
    if not _host_in_allowlist(host):
        raise UnsafeUrlError(
            f"host {host!r} is not in the allowlist "
            f"({', '.join(_allowed_hosts())})"
        )
    # Literal IP?
    try:
        _reject_ip(ipaddress.ip_address(host), host)
        return
    except ValueError:
        pass
    # Resolve and check every address.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"host {host!r} does not resolve: {exc}") from exc
    addresses: List[str] = []
    for info in infos:
        addr = info[4][0]
        addresses.append(addr)
        try:
            _reject_ip(ipaddress.ip_address(addr), host)
        except ValueError:
            raise UnsafeUrlError(
                f"host {host!r} resolves to unparsable address {addr!r}"
            )
    if not addresses:
        raise UnsafeUrlError(f"host {host!r} resolved to no addresses")


def is_safe_url(url: str) -> bool:
    """Return True when ``url`` passes every check."""
    try:
        assert_safe_url(url)
    except UnsafeUrlError:
        return False
    return True


def assert_safe_url(url: str) -> str:
    """Validate ``url``; return it unchanged or raise :class:`UnsafeUrlError`."""
    if not isinstance(url, str) or not url.strip():
        raise UnsafeUrlError("empty URL")
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UnsafeUrlError(f"unparsable URL: {exc}") from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise UnsafeUrlError(f"scheme {parts.scheme!r} is not http/https")
    if parts.username or parts.password:
        raise UnsafeUrlError("URLs with embedded credentials are not allowed")
    _check_host(parts.hostname or "")
    return url


def safe_name(name: str) -> str:
    """Turn a remote file/directory name into a safe local file name.

    Strips directory components, Windows drive/ADS syntax and reserved
    device names so a hostile name cannot escape the destination folder.
    """
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = name.split(":", 1)[0]  # drop NTFS alternate data streams
    name = name.strip().rstrip(". ") or "unnamed"
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    stem = name.split(".", 1)[0].upper()
    if stem in reserved:
        name = f"_{name}"
    return name


def safe_join(dest_dir: str, *names: str) -> str:
    """Join ``names`` under ``dest_dir``, refusing to escape it."""
    base = os.path.abspath(dest_dir)
    target = os.path.abspath(os.path.join(base, *names))
    if target != base and not target.startswith(base + os.sep):
        raise UnsafeUrlError(f"path escapes destination directory: {names!r}")
    return target


def resolve_redirect(base_url: str, location: str) -> str:
    """Resolve a possibly-relative ``Location`` header against ``base_url``."""
    return urljoin(base_url, location)
