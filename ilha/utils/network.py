"""Network helpers shared across CLI commands."""

import re
import socket

from .logging import log_info, log_warning


_IPV4_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")


def resolve_server_ip(server: str) -> str:
    """Resolve a hostname to an IPv4 address, returning the input on failure.

    If *server* already looks like a dotted-quad, it is returned unchanged.
    If DNS lookup fails, a warning is logged and *server* is returned so the
    caller can decide how to proceed (SSH will try its own resolution).
    """
    if _IPV4_RE.match(server):
        return server

    try:
        ip = socket.gethostbyname(server)
        log_info(f"Resolved {server} to {ip}")
        return ip
    except socket.gaierror:
        log_warning(f"Could not resolve {server} to IP, using as-is")
        return server
