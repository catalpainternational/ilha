#!/usr/bin/env python3
"""
Caddy Dynamic Configuration Script

This script dynamically configures Caddy routes for worktree environments.
It monitors Docker containers and updates Caddy configuration via the admin API.
"""

import json
import os
import re
import time
import requests
import docker
import logging
from typing import Dict, List, Optional

try:
    from .caddy_admin_utils import (
        build_http_to_https_redirect_route,
        default_caddy_admin_url,
        resolve_caddy_admin_url,
        running_in_container,
    )
except ImportError:
    from caddy_admin_utils import (
        build_http_to_https_redirect_route,
        default_caddy_admin_url,
        resolve_caddy_admin_url,
        running_in_container,
    )

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class CaddyDynamicConfig:
    """Dynamic Caddy configuration manager."""
    
    def __init__(self, caddy_admin_url: str = None):
        """Initialize the configuration manager."""
        # Auto-detect the correct Caddy admin URL based on execution context
        self.caddy_admin_url = caddy_admin_url or default_caddy_admin_url(
            in_container=self._is_running_in_container()
        )
        
        self.known_containers = set()
        
        # Detect Docker socket path based on environment
        docker_socket_paths = [
            '/var/run/docker.sock',  # Standard Linux path
            os.path.expanduser('~/.docker/run/docker.sock'),  # macOS Docker Desktop
            'npipe:////./pipe/docker_engine'  # Windows Docker Desktop
        ]
        
        self.docker_client = None
        for socket_path in docker_socket_paths:
            try:
                if socket_path.startswith('npipe'):
                    self.docker_client = docker.DockerClient(base_url=socket_path)
                else:
                    self.docker_client = docker.DockerClient(base_url=f'unix://{socket_path}')
                # Test connection
                self.docker_client.ping()
                logger.info(f"Connected to Docker via {socket_path}")
                break
            except Exception as e:
                logger.debug(f"Failed to connect via {socket_path}: {e}")
                self.docker_client = None
                continue
        
        if not self.docker_client:
            logger.error("Failed to connect to Docker daemon. Please ensure Docker is running.")
        else:
            self.caddy_admin_url = resolve_caddy_admin_url(
                self.caddy_admin_url,
                docker_client=self.docker_client,
            )
    
    def _is_running_in_container(self) -> bool:
        """Check if we're running inside a Docker container."""
        return running_in_container()
    
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
    
    def _is_domain(self, host: str) -> bool:
        """Check if host is a domain (not IP or localhost).
        
        Args:
            host: Host string to check
            
        Returns:
            True if host is a domain, False if IP or localhost
        """
        if host == 'localhost' or host.endswith('.localhost') or host.startswith('127.0.0.1'):
            return False
        
        # IP pattern: \d+\.\d+\.\d+\.\d+
        if re.match(r'^\d+\.\d+\.\d+\.\d+$', host):
            return False
        
        # Domain: contains dots and valid domain characters
        return '.' in host and not host.startswith('.')
    
    def check_caddy_logs_for_rate_limit(self, domain: str) -> bool:
        """Check Caddy logs for rate limit errors for a specific domain.
        
        Args:
            domain: Domain to check for rate limit errors
            
        Returns:
            True if rate limit error detected, False otherwise
        """
        try:
            if not self.docker_client:
                return False
            
            # Get Caddy container logs
            try:
                caddy_container = self.docker_client.containers.get("ilha_caddy_proxy")
                logs = caddy_container.logs(tail=100, since=300).decode('utf-8')  # Last 100 lines, last 5 minutes
                
                # Check for rate limit errors
                rate_limit_patterns = [
                    r'rateLimited',
                    r'too many certificates',
                    r'HTTP 429',
                    r'rate limit',
                    r'retry after'
                ]
                
                for pattern in rate_limit_patterns:
                    if re.search(pattern, logs, re.IGNORECASE):
                        # Also check if it's for our domain
                        if domain.lower() in logs.lower():
                            logger.warning(f"Rate limit error detected in Caddy logs for {domain}")
                            return True
            except Exception as e:
                logger.debug(f"Could not check Caddy logs: {e}")
                return False
            
            return False
        except Exception as e:
            logger.debug(f"Error checking Caddy logs for rate limit: {e}")
            return False
    
    def check_caddy_certificate_status(self, domain: str) -> Optional[str]:
        """Check current certificate status for a domain from Caddy API.
        
        Args:
            domain: Domain to check
            
        Returns:
            'production', 'staging', 'error', or None if unknown
        """
        try:
            # Get current Caddy configuration
            response = requests.get(f"{self.caddy_admin_url}/config/", timeout=5)
            if response.status_code != 200:
                return None
            
            config = response.json()
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
    
    def create_route_config(self, containers: List[Dict], use_staging: Optional[bool] = None) -> Dict:
        """Create Caddy configuration with routes for containers.
        
        Args:
            containers: List of container dictionaries with labels
            use_staging: If True, use staging ACME endpoint. If None, auto-detect from rate limits.
                        If False, use production endpoint.
        
        Returns:
            Caddy configuration dictionary
        """
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
        
        config = {
            "admin": {
                "listen": "0.0.0.0:2019",
                "enforce_origin": False,
                "origins": ["//0.0.0.0:2019"]
            },
            "apps": {
                "http": {
                    "servers": {
                        "srv0": {
                            "listen": http_listen,
                            "routes": []
                        }
                    }
                }
            }
        }
        
        # Add TLS automation if domains are present
        if has_domains:
            # Try to get CADDY_EMAIL from environment (set via env.ilha in docker-compose)
            caddy_email = os.getenv("CADDY_EMAIL")
            if not caddy_email:
                # Fallback: try to construct from first domain
                first_domain = domains[0] if domains else "example.com"
                caddy_email = f"admin@{first_domain}"
                logger.warning(f"CADDY_EMAIL not set. Using default: {caddy_email}")
            
            # Determine if we should use staging certificates
            should_use_staging = use_staging
            if should_use_staging is None:
                # Check for environment variable to force staging mode (for testing)
                env_staging = os.getenv("USE_STAGING_CERTIFICATES", "").lower()
                if env_staging in ("1", "true", "yes", "on"):
                    should_use_staging = True
                    logger.info("USE_STAGING_CERTIFICATES environment variable set, using staging certificates")
                else:
                    # Auto-detect: check for rate limit errors
                    should_use_staging = False
                    for domain in domains:
                        if self.check_caddy_logs_for_rate_limit(domain):
                            should_use_staging = True
                            logger.warning(f"Rate limit detected for {domain}, using staging certificates")
                            break
                        # Also check current certificate status
                        cert_status = self.check_caddy_certificate_status(domain)
                        if cert_status == "staging":
                            # Already using staging, keep it
                            should_use_staging = True
                            logger.info(f"Already using staging certificates for {domain}")
                            break
            
            # Build issuer configuration
            issuer_config = {
                "module": "acme",
                "email": caddy_email
            }
            
            # Add staging CA endpoint if needed
            if should_use_staging:
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
        if domains:
            routes.append(build_http_to_https_redirect_route(domains))
            logger.info(f"Added HTTP to HTTPS redirect for domains: {', '.join(domains)}")
        
        # Add routes for each container (specific routes first)
        for container in containers:
            # Labels are already in the container dict from get_docker_containers
            labels = container.get('Labels', {})
            
            if 'caddy.proxy' in labels:
                domain = labels['caddy.proxy']
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
                            "path": labels['caddy.proxy.health_check'],
                            "headers": {
                                "Host": [domain]
                            },
                            "timeout": "30s",
                            "interval": "10s"
                        }
                    }
                
                routes.append(route)
                route_type = "HTTPS" if self._is_domain(domain) else "HTTP"
                logger.info(f"Added {route_type} route for {domain} -> {target}")
        
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
    
    def update_caddy_config(self, config: Dict) -> bool:
        """Update Caddy configuration."""
        try:
            # Check if Caddy admin API is accessible
            health_response = requests.get(f"{self.caddy_admin_url}/config", timeout=5)
            if health_response.status_code != 200:
                logger.warning(f"Caddy admin API not accessible: HTTP {health_response.status_code}")
                logger.info("Falling back to container label-based routing")
                print("Using container label-based routing")
                return True  # Return True to indicate fallback is available
            
            response = requests.post(
                f"{self.caddy_admin_url}/load",
                json=config,
                timeout=10
            )
            if response.status_code == 200:
                logger.info("Successfully updated Caddy configuration via admin API")
                print("Successfully updated Caddy configuration via admin API")
                return True
            else:
                logger.warning(f"Failed to update Caddy config via admin API: HTTP {response.status_code}")
                logger.info("Trying individual route updates...")
                return self.update_routes_individually(config)
        except requests.exceptions.ConnectionError as e:
            logger.info(f"Caddy admin API not accessible at {self.caddy_admin_url}: {e}")
            logger.info("Using container label-based routing (this is normal)")
            print("Using container label-based routing")
            return True  # Return True to indicate fallback is available
        except requests.exceptions.Timeout as e:
            logger.info(f"Timeout connecting to Caddy admin API: {e}")
            logger.info("Using container label-based routing (this is normal)")
            print("Using container label-based routing")
            return True  # Return True to indicate fallback is available
        except requests.exceptions.RequestException as e:
            logger.info(f"Cannot update Caddy config via admin API: {e}")
            logger.info("Using container label-based routing (this is normal)")
            print("Using container label-based routing")
            return True  # Return True to indicate fallback is available
    
    def update_routes_individually(self, config: Dict) -> bool:
        """Update routes individually when full config update fails."""
        try:
            routes = config.get("apps", {}).get("http", {}).get("servers", {}).get("srv0", {}).get("routes", [])
            
            # Clear existing routes first
            try:
                requests.delete(f"{self.caddy_admin_url}/config/apps/http/servers/srv0/routes/", timeout=5)
                logger.info("Cleared existing routes")
            except:
                pass  # Ignore errors when clearing routes
            
            # Add each route individually
            for i, route in enumerate(routes):
                try:
                    # Skip wildcard routes (they don't have upstreams)
                    if 'handle' in route and 'upstreams' not in route['handle'][0]:
                        continue
                    
                    response = requests.post(
                        f"{self.caddy_admin_url}/config/apps/http/servers/srv0/routes/",
                        json=route,
                        headers={'Content-Type': 'application/json'},
                        timeout=5
                    )
                    
                    if response.status_code in [200, 201]:
                        hosts = route.get('match', [{}])[0].get('host', ['unknown'])
                        logger.info(f"Added route for {hosts}")
                    else:
                        logger.warning(f"Failed to add route {i}: {response.status_code}")
                        
                except Exception as e:
                    logger.warning(f"Failed to add route {i}: {e}")
            
            logger.info("Individual route updates completed")
            return True
            
        except Exception as e:
            logger.error(f"Individual route updates failed: {e}")
            return False
    
    def validate_route_configuration(self, config: Dict, containers: List[Dict]) -> bool:
        """Validate that Caddy routes match container labels correctly."""
        try:
            routes = config.get("apps", {}).get("http", {}).get("servers", {}).get("srv0", {}).get("routes", [])
            
            # Create a mapping of domain -> expected target from container labels
            expected_routes = {}
            for container in containers:
                labels = container.get('Labels', {})
                if 'caddy.proxy' in labels:
                    domain = labels['caddy.proxy']
                    expected_target = labels.get('caddy.proxy.reverse_proxy', f"{container['Names']}:8000")
                    expected_routes[domain] = expected_target
            
            # Validate each route matches expected configuration
            for route in routes:
                if 'match' in route and 'handle' in route:
                    hosts = route['match'][0].get('host', [])
                    for host in hosts:
                        if host in expected_routes:
                            # Check if the upstream target matches expected
                            handle = route['handle'][0]
                            if 'upstreams' in handle:
                                actual_target = handle['upstreams'][0].get('dial', '')
                                expected_target = expected_routes[host]
                                
                                if actual_target != expected_target:
                                    logger.error(f"Route misconfiguration detected: {host} -> {actual_target} (expected: {expected_target})")
                                    print(f"Route misconfiguration detected: {host} -> {actual_target} (expected: {expected_target})")
                                    return False
                                else:
                                    logger.info(f"Route validation passed: {host} -> {actual_target}")
                                    print(f"Route validation passed: {host} -> {actual_target}")
            
            logger.info("All route configurations validated successfully")
            print("All route configurations validated successfully")
            return True
            
        except Exception as e:
            logger.error(f"Route validation failed: {e}")
            return False
    
    def verify_upstream_connectivity(self, target: str) -> bool:
        """Verify that upstream container is reachable."""
        try:
            # Extract container name from target (e.g., "bug-web:8000" -> "bug-web")
            container_name = target.split(':')[0]
            port = target.split(':')[1] if ':' in target else '8000'
            
            # Check if container exists and is running
            if self.docker_client:
                try:
                    container = self.docker_client.containers.get(container_name)
                    if container.status == 'running':
                        logger.info(f"Upstream container {container_name} is running")
                        
                        # Test network connectivity
                        if self.test_container_network_connectivity(container_name, port):
                            logger.info(f"Network connectivity to {container_name}:{port} verified")
                            return True
                        else:
                            logger.warning(f"Network connectivity to {container_name}:{port} failed")
                            return False
                    else:
                        logger.warning(f"Upstream container {container_name} is not running (status: {container.status})")
                        return False
                except Exception as e:
                    logger.warning(f"Could not verify upstream container {container_name}: {e}")
                    return False
            else:
                logger.warning("Docker client not available for upstream verification")
                return False
                
        except Exception as e:
            logger.error(f"Upstream connectivity verification failed: {e}")
            return False
    
    def test_container_network_connectivity(self, container_name: str, port: str) -> bool:
        """Test network connectivity to a container."""
        try:
            # Get container network information
            container = self.docker_client.containers.get(container_name)
            networks = container.attrs['NetworkSettings']['Networks']
            
            # Check if container is connected to ilha_caddy_proxy network
            if 'ilha_caddy_proxy' not in networks:
                logger.warning(f"Container {container_name} is not connected to ilha_caddy_proxy network")
                return False
            
            # Get container IP in ilha_caddy_proxy network
            container_ip = networks['ilha_caddy_proxy']['IPAddress']
            if not container_ip:
                logger.warning(f"Container {container_name} has no IP address in ilha_caddy_proxy network")
                return False
            
            logger.info(f"Container {container_name} IP in ilha_caddy_proxy network: {container_ip}")
            
            # Test connectivity using a temporary container
            return self.test_network_connection(container_name, port, container_ip)
            
        except Exception as e:
            logger.error(f"Network connectivity test failed for {container_name}: {e}")
            return False
    
    def test_network_connection(self, container_name: str, port: str, container_ip: str = None) -> bool:
        """Test network connection to a container using a temporary test container."""
        try:
            import subprocess
            import time
            
            # Use container name for DNS resolution (more reliable than IP)
            test_url = f"http://{container_name}:{port}/health-check/"
            
            # Create a temporary test container to test connectivity
            test_container_name = f"caddy-connectivity-test-{int(time.time())}"
            
            # Try multiple times with increasing delays (container might be starting up)
            max_attempts = 3
            for attempt in range(max_attempts):
                try:
                    delay = attempt * 2  # 0, 2, 4 seconds
                    if delay > 0:
                        logger.info(f"Waiting {delay} seconds before connectivity test attempt {attempt + 1}")
                        time.sleep(delay)
                    
                    # Run a temporary container to test connectivity
                    result = subprocess.run([
                        'docker', 'run', '--rm', '--network', 'ilha_caddy_proxy',
                        '--name', test_container_name,
                        'curlimages/curl:latest', 'curl', '-f', '--max-time', '10', test_url
                    ], capture_output=True, text=True, timeout=15)
                    
                    if result.returncode == 0:
                        logger.info(f"Network connectivity test passed for {container_name}:{port} (attempt {attempt + 1})")
                        return True
                    else:
                        logger.warning(f"Network connectivity test failed for {container_name}:{port} (attempt {attempt + 1}) - {result.stderr}")
                        if attempt < max_attempts - 1:
                            logger.info(f"Retrying connectivity test...")
                            continue
                        else:
                            return False
                            
                except subprocess.TimeoutExpired:
                    logger.warning(f"Network connectivity test timed out for {container_name}:{port} (attempt {attempt + 1})")
                    if attempt < max_attempts - 1:
                        logger.info(f"Retrying connectivity test...")
                        continue
                    else:
                        return False
                except Exception as e:
                    logger.warning(f"Network connectivity test failed for {container_name}:{port} (attempt {attempt + 1}) - {e}")
                    if attempt < max_attempts - 1:
                        logger.info(f"Retrying connectivity test...")
                        continue
                    else:
                        return False
            
            return False
                
        except Exception as e:
            logger.error(f"Network connection test failed: {e}")
            return False
    
    def detect_misconfigurations(self, config: Dict) -> List[str]:
        """Detect routing misconfigurations in Caddy configuration."""
        misconfigurations = []
        
        try:
            routes = config.get("apps", {}).get("http", {}).get("servers", {}).get("srv0", {}).get("routes", [])
            
            for i, route in enumerate(routes):
                if 'match' in route and 'handle' in route:
                    hosts = route['match'][0].get('host', [])
                    handle = route['handle'][0]
                    
                    # Check for missing upstreams
                    if 'upstreams' not in handle:
                        misconfigurations.append(f"Route {i} has no upstreams configured")
                        continue
                    
                    # Check for invalid upstream targets
                    upstreams = handle['upstreams']
                    if not upstreams:
                        misconfigurations.append(f"Route {i} has empty upstreams list")
                        continue
                    
                    for upstream in upstreams:
                        if 'dial' not in upstream:
                            misconfigurations.append(f"Route {i} upstream missing 'dial' field")
                        elif not upstream['dial']:
                            misconfigurations.append(f"Route {i} upstream has empty 'dial' value")
                        elif not upstream['dial'].endswith(':8000'):
                            misconfigurations.append(f"Route {i} upstream target {upstream['dial']} doesn't end with :8000")
            
            if misconfigurations:
                logger.warning(f"Detected {len(misconfigurations)} routing misconfigurations")
                for misconfig in misconfigurations:
                    logger.warning(f"  - {misconfig}")
            else:
                logger.info("No routing misconfigurations detected")
                
        except Exception as e:
            logger.error(f"Misconfiguration detection failed: {e}")
            misconfigurations.append(f"Detection error: {e}")
        
        return misconfigurations

    def auto_recover_misconfigurations(self) -> bool:
        """Automatically detect and fix routing misconfigurations."""
        try:
            logger.info("Checking for routing misconfigurations...")
            
            # Get current Caddy configuration
            response = requests.get(f"{self.caddy_admin_url}/config/", timeout=5)
            if response.status_code != 200:
                logger.warning("Could not get current Caddy configuration for recovery")
                return False
            
            current_config = response.json()
            containers = self.get_docker_containers()
            
            # Check for misconfigurations
            misconfigurations_found = False
            current_routes = current_config.get("apps", {}).get("http", {}).get("servers", {}).get("srv0", {}).get("routes", [])
            
            # Create expected routes from container labels
            expected_routes = {}
            for container in containers:
                labels = container.get('Labels', {})
                if 'caddy.proxy' in labels:
                    domain = labels['caddy.proxy']
                    expected_target = labels.get('caddy.proxy.reverse_proxy', f"{container['Names']}:8000")
                    expected_routes[domain] = expected_target
            
            # Check each route for misconfigurations
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
                                    logger.warning(f"Misconfiguration detected: {host} -> {actual_target} (should be: {expected_target})")
                                    misconfigurations_found = True
            
            if misconfigurations_found:
                logger.info("Misconfigurations detected, attempting auto-recovery...")
                
                # Create correct configuration
                correct_config = self.create_route_config(containers)
                
                # Validate the correct configuration
                if not self.validate_route_configuration(correct_config, containers):
                    logger.error("Corrected configuration failed validation")
                    return False
                
                # Apply the corrected configuration
                if self.update_caddy_config(correct_config):
                    logger.info("Auto-recovery completed successfully")
                    
                    # Verify the fix
                    if self.verify_configuration_after_update(correct_config, containers):
                        logger.info("Auto-recovery verification passed")
                        return True
                    else:
                        logger.warning("Auto-recovery verification failed")
                        return False
                else:
                    logger.error("Auto-recovery failed to apply corrected configuration")
                    return False
            else:
                logger.info("No misconfigurations detected")
                return True
                
        except Exception as e:
            logger.error(f"Auto-recovery failed: {e}")
            return False

    def force_correct_routes(self, containers: List[Dict]) -> bool:
        """Force correct routes by updating each route individually."""
        try:
            logger.info("Forcing correct route configuration...")
            
            # Get expected routes from container labels
            expected_routes = {}
            for container in containers:
                labels = container.get('Labels', {})
                if 'caddy.proxy' in labels:
                    domain = labels['caddy.proxy']
                    expected_target = labels.get('caddy.proxy.reverse_proxy', f"{container['Names']}:8000")
                    expected_routes[domain] = expected_target
            
            # Update each route individually
            for domain, target in expected_routes.items():
                try:
                    # Find the route index for this domain
                    response = requests.get(f"{self.caddy_admin_url}/config/", timeout=5)
                    if response.status_code != 200:
                        continue
                    
                    current_config = response.json()
                    routes = current_config.get("apps", {}).get("http", {}).get("servers", {}).get("srv0", {}).get("routes", [])
                    
                    # Find the route for this domain
                    for i, route in enumerate(routes):
                        if 'match' in route and 'handle' in route:
                            hosts = route['match'][0].get('host', [])
                            if domain in hosts:
                                # Update this specific route
                                route['handle'][0]['upstreams'][0]['dial'] = target
                                
                                # Apply the update
                                update_response = requests.post(
                                    f"{self.caddy_admin_url}/config/apps/http/servers/srv0/routes/{i}",
                                    json=route,
                                    headers={'Content-Type': 'application/json'},
                                    timeout=5
                                )
                                
                                if update_response.status_code in [200, 201]:
                                    logger.info(f"Updated route {domain} -> {target}")
                                else:
                                    logger.warning(f"Failed to update route {domain}: {update_response.status_code}")
                                
                except Exception as e:
                    logger.warning(f"Failed to update route for {domain}: {e}")
            
            return True
            
        except Exception as e:
            logger.error(f"Force correct routes failed: {e}")
            return False

    def diagnose_network_issues(self, containers: List[Dict]) -> List[str]:
        """Diagnose network connectivity issues."""
        issues = []
        
        for container in containers:
            labels = container.get('Labels', {})
            if 'caddy.proxy' in labels:
                domain = labels['caddy.proxy']
                target = labels.get('caddy.proxy.reverse_proxy', f"{container['Names']}:8000")
                container_name = target.split(':')[0]
                
                logger.info(f"Diagnosing network issues for {domain} -> {target}")
                
                # Check container status
                try:
                    container_obj = self.docker_client.containers.get(container_name)
                    if container_obj.status != 'running':
                        issues.append(f"Container {container_name} is not running (status: {container_obj.status})")
                        continue
                except Exception as e:
                    issues.append(f"Container {container_name} not found: {e}")
                    continue
                
                # Check network connectivity
                if not self.verify_upstream_connectivity(target):
                    issues.append(f"Network connectivity failed for {container_name}:{target.split(':')[1]}")
                
                # Check container health
                if not self.check_container_health(container_name):
                    issues.append(f"Container {container_name} health check failed")
        
        if issues:
            logger.warning(f"Diagnosed {len(issues)} network issues:")
            for issue in issues:
                logger.warning(f"  - {issue}")
        else:
            logger.info("No network issues detected")
        
        return issues
    
    def check_container_health(self, container_name: str) -> bool:
        """Check container health status."""
        try:
            container = self.docker_client.containers.get(container_name)
            health_status = container.attrs.get('State', {}).get('Health', {}).get('Status', 'unknown')
            
            if health_status == 'healthy':
                logger.info(f"Container {container_name} is healthy")
                return True
            elif health_status == 'unhealthy':
                logger.warning(f"Container {container_name} is unhealthy")
                return False
            else:
                logger.info(f"Container {container_name} health status: {health_status}")
                return True  # Assume healthy if no health check defined
                
        except Exception as e:
            logger.warning(f"Could not check health for container {container_name}: {e}")
            return True  # Assume healthy if we can't check

    def configure_routes(self) -> bool:
        """Configure routes for all containers."""
        containers = self.get_docker_containers()
        config = self.create_route_config(containers)
        
        logger.info(f"Found {len(containers)} containers with caddy.proxy labels")
        
        # Diagnose network issues before configuration
        network_issues = self.diagnose_network_issues(containers)
        if network_issues:
            logger.warning(f"Network issues detected, but proceeding with configuration")
        
        # Validate configuration before applying
        if not self.validate_route_configuration(config, containers):
            logger.error("Route validation failed - configuration not applied")
            return False
        
        # Detect any misconfigurations
        misconfigurations = self.detect_misconfigurations(config)
        if misconfigurations:
            logger.warning(f"Detected {len(misconfigurations)} misconfigurations, but proceeding with update")
        
        if self.update_caddy_config(config):
            logger.info(f"Successfully configured routes for {len(containers)} containers")
            
            # Verify configuration after update
            if self.verify_configuration_after_update(config, containers):
                logger.info("Post-update verification passed")
            else:
                logger.warning("Post-update verification failed - attempting auto-recovery...")
                # Try auto-recovery if verification failed
                if self.auto_recover_misconfigurations():
                    logger.info("Auto-recovery successful")
                else:
                    logger.error("Auto-recovery failed - trying force correct routes...")
                    if self.force_correct_routes(containers):
                        logger.info("Force correct routes successful")
                    else:
                        logger.error("All recovery methods failed - routes may not work correctly")
            
            # Final network connectivity check
            if network_issues:
                logger.warning("⚠️ Network issues detected - some routes may not work correctly")
                logger.warning("Consider checking container logs and network configuration")
            
            return True
        else:
            logger.error("Failed to update Caddy configuration")
            return False
    
    def verify_configuration_after_update(self, config: Dict, containers: List[Dict]) -> bool:
        """Verify that the configuration was applied correctly after update."""
        try:
            # Wait a moment for Caddy to process the configuration
            import time
            time.sleep(2)
            
            # Get current Caddy configuration
            try:
                response = requests.get(f"{self.caddy_admin_url}/config/", timeout=5)
                if response.status_code == 200:
                    current_config = response.json()
                    
                    # Compare current config with expected config
                    current_routes = current_config.get("apps", {}).get("http", {}).get("servers", {}).get("srv0", {}).get("routes", [])
                    expected_routes = config.get("apps", {}).get("http", {}).get("servers", {}).get("srv0", {}).get("routes", [])
                    
                    if len(current_routes) != len(expected_routes):
                        logger.warning(f"Route count mismatch: current={len(current_routes)}, expected={len(expected_routes)}")
                        return False
                    
                    # Verify each route was applied correctly
                    for i, (current_route, expected_route) in enumerate(zip(current_routes, expected_routes)):
                        if current_route != expected_route:
                            logger.warning(f"Route {i} configuration mismatch")
                            return False
                    
                    logger.info("Configuration verification passed")
                    return True
                else:
                    logger.warning(f"Could not verify configuration: HTTP {response.status_code}")
                    return False
            except Exception as e:
                logger.warning(f"Configuration verification failed: {e}")
                return False
                
        except Exception as e:
            logger.error(f"Post-update verification failed: {e}")
            return False

if __name__ == "__main__":
    config_manager = CaddyDynamicConfig()
    config_manager.configure_routes()
