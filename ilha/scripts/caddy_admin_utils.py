import os
import socket
from urllib.parse import urlparse


def running_in_container() -> bool:
    """Best-effort container detection for runtime script defaults."""
    if os.path.exists("/.dockerenv"):
        return True

    try:
        with open("/proc/1/cgroup", "r") as handle:
            return "docker" in handle.read() or "containerd" in handle.read()
    except (FileNotFoundError, PermissionError, OSError):
        return False


def default_caddy_admin_url(*, in_container: bool | None = None) -> str:
    """Choose the most stable default Caddy admin URL for the runtime context."""
    if in_container is None:
        in_container = running_in_container()
    return "http://caddy:2019" if in_container else "http://localhost:2019"


def resolve_caddy_admin_url(
    url: str | None,
    *,
    docker_client=None,
    container_name: str = "ilha_caddy_proxy",
) -> str:
    """Resolve the Caddy admin URL, falling back to container IP when DNS fails."""
    candidate = url or default_caddy_admin_url()
    parsed = urlparse(candidate)
    hostname = parsed.hostname

    if not hostname or hostname == "localhost":
        return candidate

    try:
        socket.gethostbyname(hostname)
        return candidate
    except socket.gaierror:
        pass

    if docker_client is None:
        return candidate

    try:
        container = docker_client.containers.get(container_name)
    except Exception:
        return candidate

    networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
    for network_info in networks.values():
        ip_address = network_info.get("IPAddress", "")
        if ip_address:
            return candidate.replace(hostname, ip_address)

    return candidate


def build_http_to_https_redirect_route(domains: list[str]) -> dict:
    """Build a Caddy JSON route that redirects HTTP requests to HTTPS.

    The route must be ordered before reverse-proxy routes. It matches only
    plain HTTP requests for real domains, preserving path and query string.
    """
    return {
        "match": [
            {
                "host": domains,
                "protocol": "http",
            }
        ],
        "handle": [
            {
                "handler": "static_response",
                "headers": {
                    "Location": ["https://{http.request.host}{http.request.uri}"]
                },
                "status_code": 308,
            }
        ],
    }
