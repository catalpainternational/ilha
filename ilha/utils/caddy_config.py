"""
Shared Caddy configuration utility for ilha.

This module provides a single source of truth for Caddy label and network
configuration logic, used by SetupManager, EnvironmentManager, and PackageManager.
"""

from typing import Optional, Dict, Any, List

from ..utils.deploy_hosts import normalize_deploy_host
from ..utils.logging import log_info, log_warning


# Web service names that should get Caddy labels
WEB_SERVICE_NAMES = ['web', 'app', 'frontend', 'api', 'backend']

# Static / file-server image prefixes. A service whose image starts with one
# of these is treated as a routable web service even if its name is not in
# WEB_SERVICE_NAMES (e.g. a static-site project whose only service is
# `caddy: { image: caddy:2-alpine }`).
STATIC_WEB_IMAGE_PREFIXES = ('caddy', 'nginx', 'httpd')

# Default port for web services
DEFAULT_WEB_PORT = 8000


def _is_static_web_service(service_config: Dict[str, Any]) -> bool:
    """Return True if the service uses a known static/file-server image."""
    image = service_config.get('image') if isinstance(service_config, dict) else None
    if not image:
        return False
    base = str(image).lower().split(':', 1)[0].split('/', 1)[-1]
    return any(base.startswith(prefix) for prefix in STATIC_WEB_IMAGE_PREFIXES)


def _detect_service_port(
    service_config: Dict[str, Any],
    *,
    service_name: Optional[str] = None,
) -> int:
    """
    Detect the service's port from expose or ports configuration.
    
    Priority:
    1. First port from 'expose' list (container port)
    2. Container port from 'ports' mapping (e.g., "8000:8000" -> 8000)
    3. Static-image default (Caddy/Nginx/httpd → 80) when the service is
       NOT one of the conventional Django/app names. This keeps existing
       Django-style projects on port 8000 even if they happen to use nginx
       as a placeholder image, while still picking up port 80 for a static
       site whose only service is `caddy`.
    4. Default to DEFAULT_WEB_PORT (8000)
    
    Args:
        service_config: Service configuration from docker-compose.yml
        service_name: Optional service key (used to scope the static-image
            port heuristic to non-Django service names).
    
    Returns:
        Detected port number
    """
    # Check 'expose' first (container ports only, no host mapping)
    if 'expose' in service_config:
        expose_ports = service_config['expose']
        if isinstance(expose_ports, list) and expose_ports:
            # Get first exposed port
            first_port = expose_ports[0]
            if isinstance(first_port, (int, str)):
                try:
                    port = int(str(first_port).split('/')[0])  # Handle "8000/tcp" format
                    return port
                except (ValueError, AttributeError):
                    pass
    
    # Check 'ports' (host:container mappings)
    if 'ports' in service_config:
        ports = service_config['ports']
        if isinstance(ports, list) and ports:
            first_port = ports[0]
            if isinstance(first_port, str):
                # Handle "8000:8000" or "8000" format
                # Extract container port (right side of colon, or the whole thing if no colon)
                if ':' in first_port:
                    container_port = first_port.split(':')[-1]
                else:
                    container_port = first_port
                # Remove protocol suffix if present (e.g., "8000/tcp")
                container_port = container_port.split('/')[0]
                try:
                    port = int(container_port)
                    return port
                except (ValueError, AttributeError):
                    pass
            elif isinstance(first_port, int):
                return first_port

    # Static-image default — caddy/nginx/httpd typically listen on :80.
    # Skip this when the service name matches a conventional app slot
    # (web/app/api/...) to preserve the port-8000 default for Django-style
    # projects that placeholder-mount nginx without declaring expose/ports.
    if _is_static_web_service(service_config) and (
        service_name is None or service_name not in WEB_SERVICE_NAMES
    ):
        return 80

    # Default fallback
    return DEFAULT_WEB_PORT


def _has_caddy_labels(service_config: Dict[str, Any]) -> bool:
    """
    Check if a service has Caddy labels.
    
    Args:
        service_config: Service configuration from docker-compose.yml
    
    Returns:
        True if service has Caddy labels, False otherwise
    """
    if 'labels' not in service_config:
        return False
    
    labels = service_config['labels']
    
    # Handle list format
    if isinstance(labels, list):
        return any(
            isinstance(label, str) and label.startswith('caddy.proxy')
            for label in labels
        )
    
    # Handle dict format
    if isinstance(labels, dict):
        return any(
            key.startswith('caddy.proxy')
            for key in labels.keys()
        )
    
    return False


def ensure_caddy_labels_and_network(
    compose_data: Dict[str, Any],
    domain: Optional[str] = None,
    ip: Optional[str] = None,
    use_localhost_pattern: bool = True,
    path_routes: Optional[Dict[str, List[str]]] = None,
) -> bool:
    """
    Ensure Caddy labels and network are configured for web services.

    This is the single source of truth for Caddy configuration logic.
    Used by:
    - SetupManager._transform_compose_file() - during project setup
    - EnvironmentManager.apply_domain_overrides() - during domain override
    - PackageManager._standalone_import() - during package import

    Args:
        compose_data: Docker Compose data structure (dict with 'services' key)
        domain: Optional domain override (e.g., "example.com")
        ip: Optional IP override (e.g., "192.168.1.1")
        use_localhost_pattern: If True and no domain/IP, use ${COMPOSE_PROJECT_NAME}.localhost
        path_routes: Optional ``{service_name: [path1, path2, ...]}``. When set
            for a service, that service's Caddy route only matches the listed
            paths (e.g. ``/api/*``); routes without path constraints act as
            the catch-all for the same domain. The Caddy dynamic config
            sorts path-specific routes ahead of catch-alls so this works.

    Returns:
        True if any changes were made, False otherwise
    """
    if not compose_data or 'services' not in compose_data:
        return False
    
    updated = False
    
    for service_name, service_config in compose_data['services'].items():
        # Configure web services OR services that already have Caddy labels.
        # This ensures services like 'backend' with Caddy labels are processed.
        # A service running a static/file-server image (caddy/nginx/httpd) is
        # also treated as routable so Caddy-only static sites are picked up.
        is_web_service = service_name in WEB_SERVICE_NAMES
        is_static_web = _is_static_web_service(service_config)
        has_labels = _has_caddy_labels(service_config)

        if not is_web_service and not is_static_web and not has_labels:
            continue
        
        # Determine proxy domain
        if domain:
            proxy_domain = normalize_deploy_host(domain)
            log_info(f"Using production domain for {service_name}: {proxy_domain}")
        elif ip:
            proxy_domain = normalize_deploy_host(ip)
            log_warning("IP deployments are HTTP-only. Let's Encrypt requires a domain name.")
        elif use_localhost_pattern:
            # Prefer SITE_DOMAIN at compose interpolation time so public hostnames match
            # Django env (bare domain); fall back to per-project *.localhost.
            proxy_domain = "${SITE_DOMAIN:-${COMPOSE_PROJECT_NAME}.localhost}"
        else:
            # Skip if no domain/IP and localhost pattern disabled
            continue
        
        # Detect service port from expose or ports configuration
        service_port = _detect_service_port(service_config, service_name=service_name)
        
        # Get container name pattern (will be resolved by Docker Compose at runtime)
        # Pattern: ${COMPOSE_PROJECT_NAME}-{service_name}
        container_name_pattern = f"${{COMPOSE_PROJECT_NAME}}-{service_name}"
        
        # Build reverse proxy target: container_name:port
        # The Caddy dynamic config script will resolve ${COMPOSE_PROJECT_NAME} to actual container name
        reverse_proxy_target = f"{container_name_pattern}:{service_port}"
        
        # Ensure labels list exists
        existing_labels = service_config.setdefault('labels', [])
        
        # Normalize labels to list format if it's a dict
        if isinstance(existing_labels, dict):
            existing_labels = [f"{k}={v}" for k, v in existing_labels.items()]
            service_config['labels'] = existing_labels
        
        # Build proxy domain label
        proxy_domain_label = f"caddy.proxy={proxy_domain}"
        
        # Build reverse proxy label with detected port
        # We always set/update this label because port detection is critical
        reverse_proxy_label = f"caddy.proxy.reverse_proxy={reverse_proxy_target}"
        
        # Handle proxy domain label (only update on production deployments)
        proxy_domain_key = "caddy.proxy"
        proxy_domain_index = None
        for i, existing_label in enumerate(existing_labels):
            if existing_label.startswith(proxy_domain_key + '=') or existing_label == proxy_domain_key:
                proxy_domain_index = i
                break
        
        if proxy_domain_index is not None:
            # Label exists - update it only if we have a production domain/IP
            # (don't overwrite custom labels with localhost pattern)
            if domain or ip:
                old_value = existing_labels[proxy_domain_index]
                existing_labels[proxy_domain_index] = proxy_domain_label
                updated = True
                log_info(f"Updated Caddy proxy domain label for {service_name}: {old_value} -> {proxy_domain_label}")
            # else: keep existing label (localhost pattern shouldn't overwrite)
        else:
            # Label doesn't exist - add it
            existing_labels.append(proxy_domain_label)
            updated = True
            log_info(f"Added Caddy proxy domain label to {service_name}: {proxy_domain_label}")
        
        # Handle reverse proxy label (always set/update because port detection is critical)
        reverse_proxy_key = "caddy.proxy.reverse_proxy"
        reverse_proxy_index = None
        for i, existing_label in enumerate(existing_labels):
            if existing_label.startswith(reverse_proxy_key + '=') or existing_label == reverse_proxy_key:
                reverse_proxy_index = i
                break
        
        if reverse_proxy_index is not None:
            # Label exists - always update it to ensure correct port
            old_value = existing_labels[reverse_proxy_index]
            existing_labels[reverse_proxy_index] = reverse_proxy_label
            updated = True
            log_info(f"Updated Caddy reverse proxy label for {service_name}: {old_value} -> {reverse_proxy_label}")
        else:
            # Label doesn't exist - add it
            existing_labels.append(reverse_proxy_label)
            updated = True
            log_info(f"Added Caddy reverse proxy label to {service_name}: {reverse_proxy_label} (port: {service_port})")

        # Path-based routing: when this service has path constraints, emit a
        # ``caddy.proxy.path=p1,p2,...`` label. The dynamic config script
        # sorts path-specific routes ahead of path-less catch-alls so the
        # backend gets API paths and the frontend serves everything else.
        if path_routes and service_name in path_routes and path_routes[service_name]:
            paths_value = ",".join(path_routes[service_name])
            path_label = f"caddy.proxy.path={paths_value}"
            path_index = None
            for i, existing_label in enumerate(existing_labels):
                if existing_label.startswith("caddy.proxy.path="):
                    path_index = i
                    break
            if path_index is not None:
                if existing_labels[path_index] != path_label:
                    existing_labels[path_index] = path_label
                    updated = True
                    log_info(f"Updated Caddy path label for {service_name}: {path_label}")
            else:
                existing_labels.append(path_label)
                updated = True
                log_info(f"Added Caddy path label to {service_name}: {path_label}")
        elif path_routes is not None and service_name in (path_routes or {}):
            # Empty list explicitly means "drop any existing path label"
            for i, existing_label in enumerate(list(existing_labels)):
                if existing_label.startswith("caddy.proxy.path="):
                    existing_labels.pop(i)
                    updated = True
                    log_info(f"Removed Caddy path label from {service_name}")
                    break
        
        # Connect web services to ilha_caddy_proxy network
        # Important: Web containers need BOTH:
        # 1. Default network (for database/redis access)
        # 2. ilha_caddy_proxy network (for Caddy routing)
        # When networks are explicitly specified (list or dict), Docker Compose does NOT
        # automatically add the default network. We must explicitly include it.
        if 'networks' not in service_config:
            # No networks specified - explicitly include both default and ilha_caddy_proxy
            service_config['networks'] = {
                'default': None,  # Explicitly include default network for database access
                'ilha_caddy_proxy': None
            }
            updated = True
            log_info(f"Added networks to {service_name}: default + ilha_caddy_proxy")
        elif isinstance(service_config['networks'], list):
            # Convert list to dict format, preserving all existing networks
            networks_dict = {}
            for network in service_config['networks']:
                networks_dict[network] = None
            # Ensure default network is included for database access
            if 'default' not in networks_dict:
                networks_dict['default'] = None
            # Add ilha_caddy_proxy if not present
            if 'ilha_caddy_proxy' not in networks_dict:
                networks_dict['ilha_caddy_proxy'] = None
                updated = True
            service_config['networks'] = networks_dict
            if updated:
                log_info(f"Added networks to {service_name}: preserved existing + default + ilha_caddy_proxy")
        elif isinstance(service_config['networks'], dict):
            # Already in dict format - ensure both default and ilha_caddy_proxy are present
            network_added = False
            if 'default' not in service_config['networks']:
                service_config['networks']['default'] = None
                network_added = True
            if 'ilha_caddy_proxy' not in service_config['networks']:
                service_config['networks']['ilha_caddy_proxy'] = None
                network_added = True
            if network_added:
                updated = True
                log_info(f"Added networks to {service_name}: ensured default + ilha_caddy_proxy")
    
    # Ensure top-level networks declaration exists if any service references ilha_caddy_proxy
    # This is required for Docker Compose to recognize the network as external
    network_referenced = False
    for service_name, service_config in compose_data['services'].items():
        if 'networks' in service_config:
            networks = service_config['networks']
            if isinstance(networks, dict) and 'ilha_caddy_proxy' in networks:
                network_referenced = True
                break
            elif isinstance(networks, list) and 'ilha_caddy_proxy' in networks:
                network_referenced = True
                break
    
    if network_referenced:
        compose_data.setdefault('networks', {})
        if 'ilha_caddy_proxy' not in compose_data['networks']:
            compose_data['networks']['ilha_caddy_proxy'] = {'external': True}
            updated = True
            log_info("Added top-level networks declaration for ilha_caddy_proxy (external)")
    
    return updated


def update_allowed_hosts_in_compose(
    service_config: Dict[str, Any],
    domain: str
) -> bool:
    """
    Update ALLOWED_HOSTS in docker-compose.yml environment section.
    
    Sets ALLOWED_HOSTS with domain, wildcard domain, and container names.
    
    Args:
        service_config: Service configuration from docker-compose.yml
        domain: Domain to add to ALLOWED_HOSTS
    
    Returns:
        True if ALLOWED_HOSTS was updated, False otherwise
    """
    if 'environment' not in service_config:
        return False
    
    # Build complete ALLOWED_HOSTS value with domain, wildcard, and container names
    from ..core.dns_manager import get_base_domain
    base_domain = get_base_domain(domain)
    allowed_hosts = f"localhost,127.0.0.1,{domain},*.{base_domain},web"
    
    # Try to get container name from COMPOSE_PROJECT_NAME if available
    # This allows adding container-specific hostnames
    try:
        from ..config.settings import build_allowed_hosts_with_container
        # Try to extract branch name from domain or service config
        # For now, use the simple format - container names will be added if COMPOSE_PROJECT_NAME is available
        env_vars = service_config['environment']
        compose_project_name = None
        
        if isinstance(env_vars, dict):
            compose_project_name = env_vars.get('COMPOSE_PROJECT_NAME', '')
        elif isinstance(env_vars, list):
            for env_var in env_vars:
                if isinstance(env_var, str) and env_var.startswith('COMPOSE_PROJECT_NAME='):
                    compose_project_name = env_var.split('=', 1)[1].strip().strip('"\'')
                    break
        
        # If we have COMPOSE_PROJECT_NAME, try to extract branch name and build full allowed hosts
        if compose_project_name and '-' in compose_project_name:
            branch_name = compose_project_name.rsplit('-', 1)[1]
            allowed_hosts = build_allowed_hosts_with_container(branch_name, [domain, f"*.{base_domain}"])
    except Exception:
        pass  # Use simple format if extraction fails
    
    env_vars = service_config['environment']
    updated = False
    
    # Handle list format: ["ALLOWED_HOSTS=localhost,127.0.0.1", "DEBUG=True"]
    if isinstance(env_vars, list):
        for i, env_var in enumerate(env_vars):
            if isinstance(env_var, str) and env_var.startswith('ALLOWED_HOSTS='):
                # Replace existing ALLOWED_HOSTS with complete value
                env_vars[i] = f"ALLOWED_HOSTS={allowed_hosts}"
                updated = True
                log_info(f"Updated ALLOWED_HOSTS in compose environment: {allowed_hosts}")
                break
        else:
            # Add ALLOWED_HOSTS if it doesn't exist
            env_vars.append(f"ALLOWED_HOSTS={allowed_hosts}")
            updated = True
            log_info(f"Added ALLOWED_HOSTS to compose environment: {allowed_hosts}")
    
    # Handle dict format: {"ALLOWED_HOSTS": "localhost,127.0.0.1", "DEBUG": "True"}
    elif isinstance(env_vars, dict):
        env_vars['ALLOWED_HOSTS'] = allowed_hosts
        updated = True
        log_info(f"Updated ALLOWED_HOSTS in compose environment: {allowed_hosts}")
    
    return updated


def update_vite_allowed_hosts_in_compose(
    service_config: Dict[str, Any],
    domain: str,
    service_name: str
) -> bool:
    """
    Update VITE_ALLOWED_HOSTS in docker-compose.yml environment section for frontend services.
    
    This is required for Vite dev server to allow requests from production domains.
    Only applies to frontend services (detected by service name or VITE_* env vars).
    
    Args:
        service_config: Service configuration from docker-compose.yml
        domain: Domain to add to VITE_ALLOWED_HOSTS
        service_name: Name of the service (used to detect frontend services)
    
    Returns:
        True if VITE_ALLOWED_HOSTS was updated, False otherwise
    """
    # Only apply to frontend / Vite services
    if service_name not in ['frontend', 'vite']:
        # Also check if service has VITE_* environment variables (indicates Vite project)
        if 'environment' in service_config:
            env_vars = service_config['environment']
            has_vite = False
            if isinstance(env_vars, list):
                has_vite = any(isinstance(e, str) and e.startswith('VITE_') for e in env_vars)
            elif isinstance(env_vars, dict):
                has_vite = any(k.startswith('VITE_') for k in env_vars.keys())
            if not has_vite:
                return False
        else:
            return False
    
    if 'environment' not in service_config:
        # Ensure environment section exists
        service_config['environment'] = []
    
    # Build VITE_ALLOWED_HOSTS value
    from ..core.dns_manager import get_base_domain
    base_domain = get_base_domain(domain)
    vite_allowed_hosts = f"{domain},*.{base_domain},localhost,127.0.0.1"
    
    env_vars = service_config['environment']
    updated = False
    
    # Handle list format: ["VITE_API_URL=http://localhost:8000/api", "VITE_ALLOWED_HOSTS=..."]
    if isinstance(env_vars, list):
        for i, env_var in enumerate(env_vars):
            if isinstance(env_var, str) and env_var.startswith('VITE_ALLOWED_HOSTS='):
                # Replace existing VITE_ALLOWED_HOSTS
                env_vars[i] = f"VITE_ALLOWED_HOSTS={vite_allowed_hosts}"
                updated = True
                log_info(f"Updated VITE_ALLOWED_HOSTS for {service_name}: {vite_allowed_hosts}")
                break
        else:
            # Add VITE_ALLOWED_HOSTS if it doesn't exist
            # Try to add it after VITE_API_URL if present
            vite_api_idx = next((i for i, e in enumerate(env_vars) if isinstance(e, str) and e.startswith('VITE_API_URL=')), -1)
            if vite_api_idx >= 0:
                env_vars.insert(vite_api_idx + 1, f"VITE_ALLOWED_HOSTS={vite_allowed_hosts}")
            else:
                env_vars.append(f"VITE_ALLOWED_HOSTS={vite_allowed_hosts}")
            updated = True
            log_info(f"Added VITE_ALLOWED_HOSTS to {service_name}: {vite_allowed_hosts}")
    
    # Handle dict format: {"VITE_API_URL": "http://localhost:8000/api", "VITE_ALLOWED_HOSTS": "..."}
    elif isinstance(env_vars, dict):
        env_vars['VITE_ALLOWED_HOSTS'] = vite_allowed_hosts
        updated = True
        log_info(f"Updated VITE_ALLOWED_HOSTS for {service_name}: {vite_allowed_hosts}")
    
    return updated



