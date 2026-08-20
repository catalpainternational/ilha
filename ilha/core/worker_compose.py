"""Compose filtering helpers for ephemeral worker bundles.

VPC port bindings and service-subset filtering used by ``WorkerManager``.
The old hub-and-spoke ``configure_worker_environment`` path is gone.
"""

import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..utils.logging import log_info, log_warning, log_error
from ..utils.container_selector import resolve_service_dependencies


class WorkerComposeFilter:
    """Configures worker environments for distributed VPC deployments."""

    def _detect_service_ports(self, compose_data: Dict[str, Any], service_name: str) -> List[int]:
        """Extract exposed ports from service definition.

        Only returns ports from 'expose' field, not from 'ports' mappings.
        This is used to detect services that need port bindings for VPC access.

        Args:
            compose_data: Docker compose data dictionary
            service_name: Name of the service

        Returns:
            List of container port numbers from 'expose' field
        """
        service = compose_data.get('services', {}).get(service_name, {})
        ports = []

        # Only check expose (container ports) - not ports mappings
        # This method is used to detect services that need port bindings
        if 'expose' in service:
            for port in service['expose']:
                try:
                    ports.append(int(port))
                except (ValueError, TypeError):
                    continue

        return list(set(ports))  # Remove duplicates

    def _find_env_vars_for_service(self, compose_data: Dict[str, Any], service_name: str) -> List[str]:
        """Find environment variables that reference a service.

        Args:
            compose_data: Docker compose data dictionary
            service_name: Name of the service to find references for

        Returns:
            List of environment variable names that reference the service
        """
        env_vars = []
        service_upper = service_name.upper()

        # Common patterns: {SERVICE}_HOST, {SERVICE}_URL, {SERVICE}HOST, etc.
        patterns = [
            f"{service_upper}_HOST",
            f"{service_upper}_URL",
            f"{service_upper}HOST",
            f"{service_upper}URL",
            f"{service_upper}_ADDRESS",
            f"{service_upper}_SERVICE",
        ]

        # Search all services for these patterns
        for svc_name, svc_config in compose_data.get('services', {}).items():
            env = svc_config.get('environment', {})

            # Handle both dict and list formats
            if isinstance(env, dict):
                for key, value in env.items():
                    key_upper = key.upper()
                    # Check if key matches patterns
                    if any(pattern in key_upper for pattern in patterns):
                        env_vars.append(key)
                    # Also check if value contains service name
                    if isinstance(value, str) and service_name.lower() in value.lower():
                        env_vars.append(key)
            elif isinstance(env, list):
                for env_item in env:
                    if isinstance(env_item, str):
                        # Format: "KEY=value" or "KEY"
                        if '=' in env_item:
                            key = env_item.split('=', 1)[0]
                            value = env_item.split('=', 1)[1]
                        else:
                            key = env_item
                            value = None

                        key_upper = key.upper()
                        if any(pattern in key_upper for pattern in patterns):
                            env_vars.append(key)
                        if value and service_name.lower() in value.lower():
                            env_vars.append(key)

        return list(set(env_vars))  # Remove duplicates

    def configure_vpc_port_bindings(self, compose_data: Dict[str, Any],
                                    selected_services: List[str],
                                    worktree_path: Path,
                                    private_ip: Optional[str] = None) -> bool:
        """Configure port bindings for VPC-accessible services.

        Only activates when:
        - Service has expose ports but no ports mapping
        - Service is in the selected services list
        - Config allows it (opt-in via config.yml)

        Args:
            compose_data: Docker compose data dictionary (will be modified)
            selected_services: List of service names to configure
            worktree_path: Path to worktree directory (for config access)
            private_ip: Optional private IP address to bind to (defaults to 0.0.0.0 if not provided)

        Returns:
            True if any ports were configured, False otherwise
        """
        # Check if VPC port binding is enabled in config
        config_path = worktree_path / ".ilha" / "config.yml"
        auto_bind_ports = False
        bind_to_private_ip = True  # Default to true for security

        if config_path.exists():
            try:
                with open(config_path) as f:
                    config = yaml.safe_load(f) or {}
                    vpc_config = config.get('vpc', {})
                    auto_bind_ports = vpc_config.get('auto_bind_ports', False)
                    bind_to_private_ip = vpc_config.get('bind_to_private_ip', True)
            except Exception:
                pass

        if not auto_bind_ports:
            return False

        services = compose_data.get('services', {})
        configured = False

        # Determine bind address. Never fall back to 0.0.0.0: that publishes
        # the service on the public interface of a droplet that often has no
        # firewall. Missing private IP means skip bindings, not widen them.
        if not private_ip:
            log_warning(
                "vpc.auto_bind_ports is enabled but no private IP is available; "
                "skipping host port bindings rather than listening on 0.0.0.0"
            )
            return False
        if not bind_to_private_ip:
            log_warning(
                "vpc.bind_to_private_ip is false; refusing 0.0.0.0 bind. "
                "Set vpc.bind_to_private_ip: true and pass a VPC private IP."
            )
            return False
        bind_address = private_ip
        log_info(f"Binding VPC services to private IP: {private_ip}")

        for service_name in selected_services:
            if service_name not in services:
                continue

            service = services[service_name]

            # Skip if service already has ports mapping
            if 'ports' in service and service['ports']:
                continue

            # Get exposed ports
            exposed_ports = self._detect_service_ports(compose_data, service_name)

            if exposed_ports:
                # Add port bindings: {bind_address}:{container_port}:{container_port}
                port_bindings = []
                for port in exposed_ports:
                    port_bindings.append(f"{bind_address}:{port}:{port}")

                service['ports'] = port_bindings
                log_info(f"Configured VPC port bindings for {service_name}: {', '.join(port_bindings)}")
                configured = True

        return configured

    def filter_compose_services(self, compose_file: Path,
                                container_filter: List[Dict[str, str]],
                                worktree_path: Path,
                                exclude_deps: Optional[List[str]] = None,
                                private_ip: Optional[str] = None,
                                branch_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Filter compose file to include only selected services and their dependencies.

        Args:
            compose_file: Path to the compose file to filter
            container_filter: List of dicts with 'worktree' and 'container' keys
            worktree_path: Path to worktree directory (for finding branch name)
            exclude_deps: Optional list of service names to exclude from dependency resolution
            private_ip: Optional private IP address for VPC port bindings
            branch_name: Git/worktree branch to match against ``container_filter``.
                The directory name is a fallback only — a worktree folder can
                differ from the branch (``worktrees/staging-17a5ac4`` vs
                ``staging``).

        Returns:
            Filtered compose data dict, or None if filtering fails
        """
        try:
            # Load compose file
            with open(compose_file) as f:
                compose_data = yaml.safe_load(f) or {}

            services = compose_data.get('services', {})
            if not services:
                log_warning("No services found in compose file")
                return None

            dir_name = worktree_path.name
            aliases = {name for name in (branch_name, dir_name) if name}

            # Extract service names from container_filter for this branch
            selected_services = []
            for selection in container_filter:
                if selection.get('worktree') in aliases:
                    service_name = selection.get('container')
                    if service_name and service_name in services:
                        selected_services.append(service_name)

            if not selected_services:
                log_warning(
                    f"No matching services found in container filter for branch "
                    f"{branch_name or dir_name}"
                )
                return None

            log_info(f"Filtering compose file to include services: {', '.join(selected_services)}")
            if exclude_deps:
                log_info(f"Excluding services from dependencies: {', '.join(exclude_deps)}")

            # Resolve dependencies
            services_to_include = resolve_service_dependencies(compose_data, selected_services, exclude_deps)
            log_info(f"Including services with dependencies: {', '.join(services_to_include)}")

            # Configure VPC port bindings if enabled (before filtering)
            self.configure_vpc_port_bindings(compose_data, services_to_include, worktree_path, private_ip)

            # Create filtered compose data
            filtered_compose = {
                'version': compose_data.get('version'),
                'services': {},
                'networks': compose_data.get('networks', {}),
                'volumes': {}
            }

            # Copy selected services and remove depends_on for excluded services
            exclude_set = set(exclude_deps or [])
            for service_name in services_to_include:
                if service_name in services:
                    service_config = services[service_name].copy()

                    # Remove depends_on entries for excluded services
                    if 'depends_on' in service_config:
                        depends_on = service_config['depends_on']
                        if isinstance(depends_on, list):
                            # Filter out excluded services
                            filtered_deps = []
                            for dep in depends_on:
                                if isinstance(dep, str):
                                    if dep not in exclude_set:
                                        filtered_deps.append(dep)
                                elif isinstance(dep, dict):
                                    # Handle dict format: {service: {condition: ...}}
                                    dep_name = dep.get('service') or (list(dep.keys())[0] if dep else None)
                                    if dep_name and dep_name not in exclude_set:
                                        filtered_deps.append(dep)
                                else:
                                    filtered_deps.append(dep)

                            if filtered_deps:
                                service_config['depends_on'] = filtered_deps
                            else:
                                # Remove depends_on if all dependencies are excluded
                                del service_config['depends_on']
                        elif isinstance(depends_on, dict):
                            # Filter out excluded services from dict
                            filtered_deps = {k: v for k, v in depends_on.items() if k not in exclude_set}
                            if filtered_deps:
                                service_config['depends_on'] = filtered_deps
                            else:
                                del service_config['depends_on']

                    filtered_compose['services'][service_name] = service_config

            # Filter volumes to only include those used by selected services
            selected_volumes = set()
            for service_name, service_config in filtered_compose['services'].items():
                # Check volumes in service
                volumes = service_config.get('volumes', [])
                for volume in volumes:
                    if isinstance(volume, str):
                        # Parse volume string (e.g., "volume_name:/path" or "volume_name")
                        parts = volume.split(':')
                        if parts[0] and not parts[0].startswith('.') and not parts[0].startswith('/'):
                            selected_volumes.add(parts[0])
                    elif isinstance(volume, dict):
                        # Handle named volumes
                        if 'source' in volume or 'volume' in volume:
                            vol_name = volume.get('source') or volume.get('volume')
                            if vol_name and not vol_name.startswith('.') and not vol_name.startswith('/'):
                                selected_volumes.add(vol_name)

            # Copy only selected volumes
            all_volumes = compose_data.get('volumes', {})
            for vol_name in selected_volumes:
                if vol_name in all_volumes:
                    filtered_compose['volumes'][vol_name] = all_volumes[vol_name]

            return filtered_compose

        except Exception as e:
            log_error(f"Failed to filter compose services: {e}")
            return None
