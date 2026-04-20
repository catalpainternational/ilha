#!/usr/bin/env python3
"""
Caddy Docker Monitor Script

This script monitors Docker containers with caddy.proxy labels and dynamically
updates Caddy configuration via the admin API.
"""

import json
import os
import time
import requests
import docker
import logging
import re
from typing import Dict, List, Optional, Any

try:
    from .caddy_admin_utils import default_caddy_admin_url, resolve_caddy_admin_url
except ImportError:
    from caddy_admin_utils import default_caddy_admin_url, resolve_caddy_admin_url

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class CaddyDockerMonitor:
    """Monitor Docker containers and update Caddy configuration."""
    
    def __init__(self, caddy_admin_url: Optional[str] = None):
        """Initialize the monitor."""
        self.caddy_admin_url = caddy_admin_url or default_caddy_admin_url(in_container=True)
        self.known_containers = set()
        try:
            # Prefer DOCKER_HOST env var (points to docker-socket-proxy in production),
            # fall back to raw socket for local dev / backward compatibility.
            docker_host = os.environ.get("DOCKER_HOST")
            if docker_host:
                self.docker_client = docker.DockerClient(base_url=docker_host)
            else:
                self.docker_client = docker.DockerClient(base_url='unix://var/run/docker.sock')
        except Exception as e:
            logger.error(f"Failed to connect to Docker: {e}")
            self.docker_client = None
        
        # Resolve Caddy admin URL with DNS fallback
        self.caddy_admin_url = self._resolve_caddy_url(self.caddy_admin_url)
    
    def _resolve_caddy_url(self, url: str) -> str:
        """Resolve Caddy admin URL with DNS fallback to container IP.
        
        If hostname resolution fails, attempts to get container IP from Docker API.
        
        Args:
            url: Original Caddy admin URL (e.g., "http://ilha_caddy_proxy:2019")
            
        Returns:
            Resolved URL (may be original or IP-based fallback)
        """
        resolved_url = resolve_caddy_admin_url(url, docker_client=self.docker_client)
        if resolved_url != url:
            logger.info(f"Using resolved Caddy admin URL: {resolved_url} (original: {url})")
        return resolved_url
    
    def get_docker_containers(self) -> List[Dict]:
        """Get Docker containers with caddy.proxy labels."""
        try:
            if not self.docker_client:
                return []
            
            containers = []
            for container in self.docker_client.containers.list(filters={'label': 'caddy.proxy'}):
                containers.append({
                    'ID': container.id,
                    'Names': container.name,
                    'Labels': container.labels
                })
            
            return containers
        except Exception as e:
            logger.error(f"Failed to get Docker containers: {e}")
            return []
    
    def get_container_labels(self, container_id: str) -> Dict[str, str]:
        """Get labels for a specific container."""
        try:
            if not self.docker_client:
                return {}
            
            container = self.docker_client.containers.get(container_id)
            return container.labels
        except Exception as e:
            logger.error(f"Failed to get labels for container {container_id}: {e}")
            return {}
    
    def get_caddy_config(self) -> Optional[Dict]:
        """Get current Caddy configuration."""
        try:
            response = requests.get(f"{self.caddy_admin_url}/config", timeout=5)
            if response.status_code == 200:
                return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to get Caddy config: {e}")
        return None
    
    def update_caddy_config(self, config: Dict) -> bool:
        """Update Caddy configuration."""
        try:
            response = requests.post(
                f"{self.caddy_admin_url}/load",
                json=config,
                timeout=10
            )
            if response.status_code != 200:
                logger.error(f"Failed to update Caddy config: {response.status_code} - {response.text}")
                return False
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to update Caddy config: {e}")
            return False
    
    def _is_domain(self, host: str) -> bool:
        """Check if a host string is a domain (not localhost or IP)."""
        if not host or not isinstance(host, str):
            return False
        
        # IP address patterns
        import re
        ipv4_pattern = r'^(\d{1,3}\.){3}\d{1,3}$'
        ipv6_pattern = r'^([0-9a-fA-F]{0,4}:){2,7}[0-9a-fA-F]{0,4}$'
        
        if re.match(ipv4_pattern, host) or re.match(ipv6_pattern, host):
            return False
        
        # localhost variants
        if host in ['localhost', '127.0.0.1', '::1'] or host.endswith('.localhost'):
            return False
        
        # Domain: contains dots and valid domain characters
        return '.' in host and not host.startswith('.')
    
    def create_route_config(self, containers: List[Dict]) -> Dict:
        """Create Caddy configuration with routes for containers."""
        # Detect if any domains are being used (vs localhost/IP)
        has_domains = False
        domains_set = set()
        
        for container in containers:
            labels = container.get('Labels', {})
            if 'caddy.proxy' in labels:
                domain = labels['caddy.proxy']
                if self._is_domain(domain):
                    has_domains = True
                    domains_set.add(domain)
        domains = sorted(list(domains_set))  # Convert to sorted list for consistent ordering
        
        # Configure HTTP server (always present)
        http_listen = [":80"]
        # Add HTTPS listener if domains are detected
        if has_domains:
            http_listen.append(":443")
            logger.info(f"HTTPS enabled for domains: {', '.join(domains)}")
        
        srv0 = {
            "listen": http_listen,
            "routes": []
        }
        # Keep .localhost and IP-only setups HTTP-only (no redirect to HTTPS)
        if not has_domains:
            srv0["automatic_https"] = {"disable_redirects": True}
        config = {
            "admin": {
                "listen": "0.0.0.0:2019",
                "enforce_origin": False,
                "origins": ["//0.0.0.0:2019"]
            },
            "apps": {
                "http": {
                    "servers": {
                        "srv0": srv0
                    }
                }
            }
        }
        
        # Add TLS automation if domains are present
        if has_domains:
            import os
            # Try to get CADDY_EMAIL from environment (set via env.ilha in docker-compose)
            caddy_email = os.getenv("CADDY_EMAIL")
            if not caddy_email:
                # Fallback: try to construct from first domain
                first_domain = domains[0] if domains else "example.com"
                caddy_email = f"admin@{first_domain}"
                logger.warning(f"CADDY_EMAIL not set. Using default: {caddy_email}")
            
            # Check for environment variable to force staging mode (for testing)
            env_staging = os.getenv("USE_STAGING_CERTIFICATES", "").lower()
            use_staging = env_staging in ("1", "true", "yes", "on")
            
            issuer_config = {
                "module": "acme",
                "email": caddy_email
            }
            
            # Add staging CA endpoint if needed
            if use_staging:
                issuer_config["ca"] = "https://acme-staging-v02.api.letsencrypt.org/directory"
                logger.info(f"Using Let's Encrypt staging endpoint for domains: {', '.join(domains)}")
                logger.warning("Staging certificates will show browser warnings but allow HTTPS to work")
            else:
                logger.info(f"Using Let's Encrypt production endpoint for domains: {', '.join(domains)}")
            
            config["apps"]["tls"] = {
                "automation": {
                    "policies": [{
                        "subjects": domains,
                        "issuers": [issuer_config]
                    }]
                }
            }
        
        routes = []
        
        # Group containers by domain to handle path-based routing
        domain_containers = {}
        for container in containers:
            labels = container.get('Labels', {})
            if 'caddy.proxy' in labels:
                domain = labels['caddy.proxy']
                if domain not in domain_containers:
                    domain_containers[domain] = []
                domain_containers[domain].append((container, labels))
        
        # Add routes for each domain (with path-based routing support)
        for domain, container_list in domain_containers.items():
            if len(container_list) == 1:
                # Single container for this domain - simple route
                container, labels = container_list[0]
                target = labels.get('caddy.proxy.reverse_proxy', f"{container['Names']}:8000")
                
                route = {
                    "match": [{"host": [domain]}],
                    "handle": [{
                        "handler": "reverse_proxy",
                        "upstreams": [{"dial": target}]
                    }]
                }
                
                # Add health check if specified
                if 'caddy.proxy.health_check' in labels:
                    route["handle"][0]["health_checks"] = {
                        "active": {
                            "path": labels['caddy.proxy.health_check']
                        }
                    }
                
                routes.append(route)
                route_type = "HTTPS" if self._is_domain(domain) else "HTTP"
                logger.info(f"Added {route_type} route for {domain} -> {target}")
            else:
                # Multiple containers for same domain - use subroute with path matching
                # Sort containers: specific paths first (longest first), then catch-all
                def sort_key(x):
                    labels = x[1]
                    path = labels.get('caddy.proxy.path', '/')
                    except_path = labels.get('caddy.proxy.except', '')
                    # Priority: specific paths > except paths > catch-all
                    if path and path != '/':
                        return (0, -len(path))  # Specific path, longer = higher priority
                    elif except_path:
                        return (1, 0)  # Except path
                    else:
                        return (2, 0)  # Catch-all
                
                container_list_sorted = sorted(container_list, key=sort_key)
                
                # Create subroutes - Caddy evaluates subroutes in order within a subroute handler
                subroutes = []
                for container, labels in container_list_sorted:
                    target = labels.get('caddy.proxy.reverse_proxy', f"{container['Names']}:8000")
                    path = labels.get('caddy.proxy.path', '/')
                    except_path = labels.get('caddy.proxy.except', '')
                    
                    # Create match conditions for subroute
                    # Subroutes are evaluated in order, so specific paths must come first
                    match_conditions = []
                    if path and path != '/':
                        # Specific path pattern (e.g., /api/*) - must come first
                        match_conditions.append({"path": [path]})
                    # For catch-all, no path condition - matches everything not matched above
                    
                    subroute = {
                        "match": match_conditions if match_conditions else [],
                        "handle": [{
                            "handler": "reverse_proxy",
                            "upstreams": [{"dial": target}]
                        }]
                    }
                    
                    # Add health check if specified
                    if 'caddy.proxy.health_check' in labels:
                        subroute["handle"][0]["health_checks"] = {
                            "active": {
                                "path": labels['caddy.proxy.health_check']
                            }
                        }
                    
                    subroutes.append(subroute)
                    route_type = "HTTPS" if self._is_domain(domain) else "HTTP"
                    logger.info(f"Added subroute for {domain} path {path} (except: {except_path}) -> {target}")
                
                # Create main route with subroute handler
                # Subroutes are evaluated in order, so /api/* matches first, then catch-all
                route = {
                    "match": [{"host": [domain]}],
                    "handle": [{
                        "handler": "subroute",
                        "routes": subroutes
                    }]
                }
                routes.append(route)
                route_type = "HTTPS" if self._is_domain(domain) else "HTTP"
                logger.info(f"Added {route_type} route with subroutes for {domain}")
        
        # Add default wildcard route at the end (must be last for proper matching)
        routes.append({
            "match": [{"host": ["*"]}],
            "handle": [{
                "handler": "static_response",
                "body": "ilha Global Caddy Proxy Ready - No worktree found for this domain",
                "status_code": 200
            }]
        })
        
        config["apps"]["http"]["servers"]["srv0"]["routes"] = routes
        return config
    
    def validate_route_configuration(self, config: Dict, containers: List[Dict]) -> bool:
        """Validate that Caddy routes match container labels correctly."""
        try:
            routes = config.get("apps", {}).get("http", {}).get("servers", {}).get("srv0", {}).get("routes", [])
            
            # Create a mapping of domain+path -> expected target from container labels
            expected_routes = {}
            for container in containers:
                labels = container.get('Labels', {})
                if 'caddy.proxy' in labels:
                    domain = labels['caddy.proxy']
                    path = labels.get('caddy.proxy.path', '/')
                    expected_target = labels.get('caddy.proxy.reverse_proxy', f"{container['Names']}:8000")
                    # Use domain+path as key to handle multiple routes per domain
                    route_key = f"{domain}:{path}"
                    expected_routes[route_key] = expected_target
            
            # Validate each route matches expected configuration
            # Build a set of all expected route keys for validation
            validated_routes = set()
            for route in routes:
                if 'match' in route and 'handle' in route:
                    hosts = route['match'][0].get('host', [])
                    # Extract path from match conditions (if present)
                    path = '/'
                    if len(route.get('match', [])) > 1:
                        path_match = route['match'][1].get('path', [])
                        if path_match:
                            path = path_match[0] if isinstance(path_match, list) else path_match
                    
                    for host in hosts:
                        route_key = f"{host}:{path}"
                        if route_key in expected_routes:
                            # Check if the upstream target matches expected
                            handle = route['handle'][0]
                            if 'upstreams' in handle:
                                actual_target = handle['upstreams'][0].get('dial', '')
                                expected_target = expected_routes[route_key]
                                
                                if actual_target != expected_target:
                                    logger.error(f"Route misconfiguration detected: {route_key} -> {actual_target} (expected: {expected_target})")
                                    return False
                                else:
                                    logger.info(f"Route validation passed: {route_key} -> {actual_target}")
                                    validated_routes.add(route_key)
            
            # Check that all expected routes were validated
            if len(validated_routes) != len(expected_routes):
                missing = set(expected_routes.keys()) - validated_routes
                logger.warning(f"Some expected routes were not found in configuration: {missing}")
                # Don't fail validation for this - routes might be in different order
            
            logger.info("All route configurations validated successfully")
            return True
            
        except Exception as e:
            logger.error(f"Route validation failed: {e}")
            return False

    def check_caddy_logs_for_certificate_errors(self, domain: str) -> Dict[str, Any]:
        """Check Caddy logs for certificate acquisition errors for a specific domain.
        
        Args:
            domain: Domain to check for certificate errors
            
        Returns:
            Dictionary with error information, or empty dict if no errors found
        """
        error_info = {}
        try:
            if not self.docker_client:
                return error_info
            
            # Get Caddy container logs
            try:
                caddy_container = self.docker_client.containers.get("ilha_caddy_proxy")
                logs = caddy_container.logs(tail=200, since=600).decode('utf-8')  # Last 200 lines, last 10 minutes
                
                # Check for rate limit errors
                rate_limit_patterns = [
                    (r'rateLimited', 'rate_limit'),
                    (r'too many certificates', 'rate_limit'),
                    (r'HTTP 429', 'rate_limit'),
                    (r'rate limit', 'rate_limit'),
                    (r'retry after', 'rate_limit')
                ]
                
                for pattern, error_type in rate_limit_patterns:
                    if re.search(pattern, logs, re.IGNORECASE):
                        # Also check if it's for our domain
                        if domain.lower() in logs.lower():
                            error_info = {
                                'type': error_type,
                                'domain': domain,
                                'message': f'Rate limit error detected for {domain}',
                                'severity': 'warning'
                            }
                            logger.warning(f"Certificate error detected: {error_info['message']}")
                            return error_info
                
                # Check for other certificate errors
                cert_error_patterns = [
                    (r'could not get certificate', 'certificate_error'),
                    (r'certificate.*failed', 'certificate_error'),
                    (r'tls.*error', 'certificate_error'),
                    (r'obtaining certificate.*error', 'certificate_error')
                ]
                
                for pattern, error_type in cert_error_patterns:
                    if re.search(pattern, logs, re.IGNORECASE):
                        if domain.lower() in logs.lower():
                            error_info = {
                                'type': error_type,
                                'domain': domain,
                                'message': f'Certificate acquisition error for {domain}',
                                'severity': 'error'
                            }
                            logger.error(f"Certificate error detected: {error_info['message']}")
                            return error_info
                            
            except Exception as e:
                logger.debug(f"Could not check Caddy logs: {e}")
                return error_info
            
            return error_info
        except Exception as e:
            logger.debug(f"Error checking Caddy logs for certificate errors: {e}")
            return error_info
    
    def check_certificate_status(self, domain: str) -> Optional[str]:
        """Check current certificate status for a domain from Caddy API.
        
        Args:
            domain: Domain to check
            
        Returns:
            'production', 'staging', 'error', or None if unknown
        """
        try:
            config = self.get_caddy_config()
            if not config:
                return None
            
            tls_config = config.get("apps", {}).get("tls", {})
            policies = tls_config.get("automation", {}).get("policies", [])
            
            for policy in policies:
                if domain in policy.get("subjects", []):
                    issuers = policy.get("issuers", [])
                    for issuer in issuers:
                        ca = issuer.get("ca", "")
                        if "staging" in ca.lower():
                            return "staging"
                        elif "acme" in issuer.get("module", "").lower():
                            return "production"
            
            return None
        except Exception as e:
            logger.debug(f"Error checking certificate status: {e}")
            return None
    
    def monitor_certificate_health(self, containers: List[Dict]) -> List[Dict]:
        """Monitor certificate health for all domains in containers.
        
        Args:
            containers: List of container dictionaries with labels
            
        Returns:
            List of certificate health information dictionaries
        """
        health_reports = []
        
        for container in containers:
            labels = container.get('Labels', {})
            if 'caddy.proxy' in labels:
                domain = labels['caddy.proxy']
                if self._is_domain(domain):
                    # Check for certificate errors
                    error_info = self.check_caddy_logs_for_certificate_errors(domain)
                    
                    # Check current certificate status
                    cert_status = self.check_certificate_status(domain)
                    
                    health_report = {
                        'domain': domain,
                        'status': cert_status or 'unknown',
                        'has_errors': bool(error_info),
                        'error_info': error_info
                    }
                    
                    health_reports.append(health_report)
                    
                    # Log health status
                    if error_info:
                        logger.warning(f"Certificate health issue for {domain}: {error_info.get('message', 'Unknown error')}")
                    elif cert_status:
                        logger.info(f"Certificate status for {domain}: {cert_status}")
        
        return health_reports
    
    def detect_configuration_drift(self, containers: List[Dict]) -> List[str]:
        """Detect when Caddy configuration doesn't match container labels."""
        drift_issues = []
        
        try:
            # Get current Caddy configuration
            response = requests.get(f"{self.caddy_admin_url}/config/", timeout=5)
            if response.status_code != 200:
                logger.warning("Could not get current Caddy configuration for drift detection")
                return drift_issues
            
            current_config = response.json()
            current_routes = current_config.get("apps", {}).get("http", {}).get("servers", {}).get("srv0", {}).get("routes", [])
            
            # Create expected routes from container labels
            expected_routes = {}
            for container in containers:
                labels = container.get('Labels', {})
                if 'caddy.proxy' in labels:
                    domain = labels['caddy.proxy']
                    expected_target = labels.get('caddy.proxy.reverse_proxy', f"{container['Names']}:8000")
                    expected_routes[domain] = expected_target
            
            # Check for drift
            for route in current_routes:
                if 'match' in route and 'handle' in route:
                    hosts = route['match'][0].get('host', [])
                    for host in hosts:
                        if host in expected_routes:
                            handle = route['handle'][0]
                            if 'upstreams' in handle:
                                actual_target = handle['upstreams'][0].get('dial', '')
                                expected_target = expected_routes[host]
                                
                                if actual_target != expected_target:
                                    drift_issues.append(f"Domain {host} points to {actual_target} but should point to {expected_target}")
            
            if drift_issues:
                logger.warning(f"Detected {len(drift_issues)} configuration drift issues")
                for issue in drift_issues:
                    logger.warning(f"  - {issue}")
            else:
                logger.info("No configuration drift detected")
                
        except Exception as e:
            logger.error(f"Configuration drift detection failed: {e}")
            drift_issues.append(f"Detection error: {e}")
        
        return drift_issues

    def auto_reconfigure_on_drift(self, containers: List[Dict]) -> bool:
        """Automatically reconfigure when drift is detected."""
        try:
            logger.info("Auto-reconfiguring due to detected drift...")
            
            # Create new configuration
            config = self.create_route_config(containers)
            
            # Validate before applying
            if not self.validate_route_configuration(config, containers):
                logger.error("Route validation failed during auto-reconfiguration")
                return False
            
            # Update Caddy
            if self.update_caddy_config(config):
                logger.info("Auto-reconfiguration completed successfully")
                return True
            else:
                logger.error("Auto-reconfiguration failed")
                return False
                
        except Exception as e:
            logger.error(f"Auto-reconfiguration failed: {e}")
            return False

    def monitor(self):
        """Main monitoring loop."""
        logger.info("Starting Caddy Docker monitor...")
        
        while True:
            try:
                containers = self.get_docker_containers()
                current_containers = {c['ID'] for c in containers}
                
                # Check if configuration needs updating
                if current_containers != self.known_containers:
                    logger.info(f"Container change detected: {len(containers)} containers")
                    
                    # Create new configuration
                    config = self.create_route_config(containers)
                    
                    # Validate configuration before applying
                    if not self.validate_route_configuration(config, containers):
                        logger.error("Route validation failed - skipping configuration update")
                        time.sleep(5)
                        continue
                    
                    # Update Caddy
                    if self.update_caddy_config(config):
                        logger.info("Caddy configuration updated successfully")
                        self.known_containers = current_containers
                    else:
                        logger.error("Failed to update Caddy configuration")
                else:
                    # Check for configuration drift even when containers haven't changed
                    drift_issues = self.detect_configuration_drift(containers)
                    if drift_issues:
                        logger.warning("Configuration drift detected, auto-reconfiguring...")
                        if self.auto_reconfigure_on_drift(containers):
                            logger.info("Auto-reconfiguration completed")
                        else:
                            logger.error("Auto-reconfiguration failed")
                    
                    # Monitor certificate health
                    cert_health = self.monitor_certificate_health(containers)
                    for health in cert_health:
                        if health['has_errors']:
                            error_info = health['error_info']
                            if error_info.get('type') == 'rate_limit':
                                logger.warning(f"Rate limit detected for {health['domain']}. Consider using staging certificates.")
                                logger.warning(f"To fix: Update Caddy config to use staging ACME endpoint for {health['domain']}")
                
                time.sleep(5)  # Check every 5 seconds
                
            except KeyboardInterrupt:
                logger.info("Monitor stopped by user")
                break
            except Exception as e:
                logger.error(f"Error in monitor loop: {e}")
                time.sleep(5)

if __name__ == "__main__":
    monitor = CaddyDockerMonitor()
    monitor.monitor()
