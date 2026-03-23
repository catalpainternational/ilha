from urllib.parse import urlsplit


def normalize_deploy_host(value: str) -> str:
    """Return a bare host/IP for deploy settings and Caddy labels."""
    host = (value or "").strip().strip('"').strip("'")
    if not host:
        return ""

    if "://" in host:
        parsed = urlsplit(host)
        host = parsed.netloc or parsed.path

    host = host.strip().rstrip("/")
    if "/" in host:
        host = host.split("/", 1)[0]

    return host.strip()


def build_csrf_trusted_origins(
    host: str,
    *,
    base_domain: str | None = None,
    include_http: bool = True,
    include_https: bool = True,
    include_wildcard_https: bool = True,
) -> str:
    """Build a comma-separated CSRF_TRUSTED_ORIGINS value from a bare host."""
    normalized_host = normalize_deploy_host(host)
    normalized_base_domain = normalize_deploy_host(base_domain or "")
    origins: list[str] = []

    if normalized_host:
        if include_https:
            origins.append(f"https://{normalized_host}")
        if include_http:
            origins.append(f"http://{normalized_host}")

    if normalized_base_domain and include_wildcard_https:
        origins.append(f"https://*.{normalized_base_domain}")

    return ",".join(dict.fromkeys(origins))
