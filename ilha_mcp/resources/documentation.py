"""
Static documentation resources for ilha MCP server.

This module provides comprehensive static documentation about ilha
concepts, architecture, workflows, and terminology for AI agents.
"""

from typing import Dict, Any, List


class ilhaDocumentation:
    """Static documentation about ilha concepts and usage."""
    
    @staticmethod
    def get_concept() -> Dict[str, Any]:
        """Get comprehensive ilha concept explanation."""
        return {
            "name": "ilha",
            "description": "ilha creates isolated development environments for Git branches using Git worktrees, Docker Compose, and Caddy proxy",
            "key_benefits": [
                "Complete environment isolation - each branch gets its own database, Redis, and media storage",
                "No port conflicts - uses dynamic routing with Caddy proxy instead of port mapping",
                "Easy parallel development - work on multiple features simultaneously",
                "Safe database testing - test migrations and data changes in isolation",
                "Simple cleanup - remove environments without affecting other branches"
            ],
            "core_components": {
                "git_worktrees": "Isolated working directories for each branch - separate from main working directory",
                "docker_compose": "Branch-specific containers with isolated volumes and networks",
                "caddy_proxy": "Global reverse proxy that routes requests to correct worktree based on subdomain",
                "volume_isolation": "Branch-specific volumes ensure complete data separation"
            },
            "url_pattern": "Each worktree gets a unique URL: http://{project-name}-{branch-name}.localhost",
            "typical_use_cases": [
                "Feature development with isolated database",
                "Testing database migrations safely",
                "Parallel development on multiple features",
                "Code review with live environment",
                "Client demos with specific feature branches"
            ]
        }
    
    @staticmethod
    def get_architecture() -> Dict[str, Any]:
        """Get detailed technical architecture explanation."""
        return {
            "overview": "ilha combines three core technologies for complete environment isolation",
            "components": {
                "git_worktrees": {
                    "purpose": "Isolated working directories for each branch",
                    "location": ".ilha/worktrees/{branch-name}/",
                    "benefits": "Separate codebase for each branch without affecting main directory"
                },
                "docker_compose": {
                    "purpose": "Containerized services with branch-specific configuration",
                    "override_file": "docker-compose.ilha.yml",
                    "networks": "ilha_caddy_proxy (external), {branch}_internal, {branch}_web",
                    "volumes": "{branch_name}_{volume_type} (e.g., feature-auth_postgres_data)"
                },
                "caddy_proxy": {
                    "purpose": "Global reverse proxy for dynamic routing",
                    "container": "ilha_caddy_proxy",
                    "routing": "Routes *.localhost to appropriate worktree containers",
                    "benefits": "No port conflicts, automatic service discovery"
                }
            },
            "data_flow": [
                "1. Request comes to http://project-branch.localhost",
                "2. Caddy proxy matches subdomain to worktree",
                "3. Proxy routes to worktree's web container",
                "4. Web container connects to worktree's database/Redis",
                "5. All data is isolated to that specific worktree"
            ],
            "volume_naming": {
                "pattern": "{project_name}_{branch_name}_{volume_type}",
                "examples": [
                    "myapp_feature-auth_postgres_data",
                    "myapp_feature-auth_redis_data",
                    "myapp_feature-auth_media_files"
                ],
                "isolation": "Each worktree has completely separate data - no cross-contamination"
            },
            "network_architecture": {
                "global_network": "ilha_caddy_proxy (external, shared by all worktrees)",
                "worktree_networks": [
                    "{branch_name}_internal (database, Redis, internal services)",
                    "{branch_name}_web (web application, external access)"
                ],
                "routing": "Caddy proxy connects to worktree networks for routing"
            }
        }
    
    @staticmethod
    def get_workflow_patterns() -> Dict[str, Any]:
        """Get common workflow patterns with detailed steps."""
        return {
            "feature_development": {
                "description": "Complete feature development lifecycle",
                "steps": [
                    "1. create_worktree('feature-auth') - Create isolated environment",
                    "2. start_worktree('feature-auth') - Launch containers and services",
                    "3. Access at http://project-feature-auth.localhost - Develop with isolated database",
                    "4. stop_worktree('feature-auth') - Stop when done (data preserved)",
                    "5. remove_worktree('feature-auth') - Clean up when feature complete"
                ],
                "benefits": "Safe development with isolated data, easy cleanup"
            },
            "multiple_features": {
                "description": "Working on multiple features simultaneously",
                "steps": [
                    "1. create_worktree('feature-auth')",
                    "2. create_worktree('feature-payments')", 
                    "3. start_worktree('feature-auth')",
                    "4. start_worktree('feature-payments')",
                    "5. Access both: http://project-feature-auth.localhost and http://project-feature-payments.localhost"
                ],
                "benefits": "No conflicts between features, independent development"
            },
            "database_testing": {
                "description": "Testing database migrations in isolation",
                "steps": [
                    "1. create_worktree('test-migration')",
                    "2. start_worktree('test-migration')",
                    "3. Test migrations with isolated database",
                    "4. delete_worktree('test-migration') - Clean up test environment"
                ],
                "benefits": "Safe migration testing, no risk to main database"
            },
            "code_review": {
                "description": "Code review with live environment",
                "steps": [
                    "1. create_worktree('pr-123')",
                    "2. start_worktree('pr-123')",
                    "3. Reviewer accesses http://project-pr-123.localhost",
                    "4. Test functionality in isolated environment",
                    "5. remove_worktree('pr-123') - Clean up after review"
                ],
                "benefits": "Live testing during code review, isolated from other work"
            },
            "client_demo": {
                "description": "Client demo with specific feature",
                "steps": [
                    "1. create_worktree('demo-feature')",
                    "2. start_worktree('demo-feature')",
                    "3. Client accesses http://project-demo-feature.localhost",
                    "4. Demo specific functionality",
                    "5. remove_worktree('demo-feature') - Clean up after demo"
                ],
                "benefits": "Clean demo environment, no interference from other work"
            }
        }
    
    @staticmethod
    def get_terminology() -> Dict[str, Any]:
        """Get glossary of ilha-specific terms."""
        return {
            "worktree": {
                "definition": "Isolated working directory for a Git branch",
                "ilha_context": "Each worktree gets its own Docker environment with isolated data",
                "location": ".ilha/worktrees/{branch-name}/"
            },
            "isolated_environment": {
                "definition": "Complete development environment with isolated containers and data",
                "components": ["Git worktree", "Docker containers", "Branch-specific volumes", "Unique URL"],
                "benefits": "No conflicts with other branches or main development"
            },
            "branch_specific_volumes": {
                "definition": "Docker volumes that contain data specific to a branch",
                "naming": "{project_name}_{branch_name}_{volume_type}",
                "examples": ["myapp_feature-auth_postgres_data", "myapp_feature-auth_redis_data"],
                "isolation": "Complete data separation between branches"
            },
            "caddy_proxy": {
                "definition": "Global reverse proxy that routes requests to worktrees",
                "routing": "Routes *.localhost to appropriate worktree containers",
                "benefits": "No port conflicts, automatic service discovery"
            },
            "dynamic_routing": {
                "definition": "Automatic routing based on subdomain patterns",
                "pattern": "http://{project-name}-{branch-name}.localhost",
                "benefits": "Each worktree gets unique URL without configuration"
            },
            "volume_isolation": {
                "definition": "Complete separation of data between worktrees",
                "implementation": "Branch-specific volume names and Docker networks",
                "benefits": "No data conflicts, safe testing, easy cleanup"
            },
            "worktree_lifecycle": {
                "definition": "Complete lifecycle of a ilha environment",
                "stages": ["create", "start", "develop", "stop", "remove/delete"],
                "data_preservation": "Data preserved between start/stop cycles"
            }
        }
    
    @staticmethod
    def get_url_patterns() -> Dict[str, Any]:
        """Get URL construction and routing patterns."""
        return {
            "pattern": "http://{project-name}-{branch-name}.localhost",
            "examples": [
                "http://myapp-feature-auth.localhost",
                "http://myapp-bugfix-payment.localhost", 
                "http://myapp-pr-123.localhost"
            ],
            "routing_logic": {
                "subdomain_extraction": "Extract branch name from subdomain pattern",
                "worktree_matching": "Match branch name to existing worktree",
                "container_routing": "Route to worktree's web container"
            },
            "caddy_configuration": {
                "automatic": "Caddy automatically detects worktree containers",
                "labels": "Containers labeled with branch information",
                "dynamic": "No manual configuration needed for new worktrees"
            },
            "development_benefits": [
                "No port conflicts - each worktree uses same internal ports",
                "Easy access - predictable URL pattern",
                "Automatic routing - no manual configuration",
                "SSL support - automatic HTTPS with localhost certificates"
            ],
            "troubleshooting": {
                "url_not_working": [
                    "Check if worktree is running: get_worktree_info()",
                    "Verify Caddy proxy is running: get_proxy_status()",
                    "Check container labels and network connectivity"
                ],
                "wrong_worktree": [
                    "Verify branch name in URL matches worktree",
                    "Check worktree status and container health",
                    "Restart worktree if needed: stop_worktree() then start_worktree()"
                ]
            }
        }
    
    @staticmethod
    def get_best_practices() -> Dict[str, Any]:
        """Get ilha best practices and recommendations."""
        return {
            "naming_conventions": {
                "branches": "Use descriptive branch names (feature-auth, bugfix-payment)",
                "worktrees": "Branch names become worktree names automatically",
                "urls": "URLs follow pattern: project-branch.localhost"
            },
            "workflow_recommendations": {
                "feature_development": "Create worktree → Start environment → Develop → Stop → Remove when done",
                "testing": "Use separate worktrees for testing different scenarios",
                "code_review": "Create worktree for PR review, remove after review",
                "cleanup": "Regular cleanup of old worktrees to save disk space"
            },
            "performance_considerations": {
                "disk_usage": "Each worktree uses ~5GB disk space (varies with data)",
                "memory": "Each worktree uses ~1GB RAM when running",
                "startup_time": "Worktree creation: ~30s, Container startup: ~45s",
                "optimization": "Stop unused worktrees to save resources"
            },
            "troubleshooting_tips": {
                "common_issues": [
                    "Docker not running - check docker ps",
                    "Port conflicts - use Caddy proxy instead of port mapping",
                    "Volume issues - check Docker disk space and permissions",
                    "Network issues - verify ilha_caddy_proxy network exists"
                ],
                "debugging": [
                    "Check container logs: docker logs {container-name}",
                    "Verify worktree status: get_worktree_info()",
                    "Check proxy status: get_proxy_status()",
                    "Inspect volumes: list_volumes()"
                ]
            },
            "security_considerations": {
                "isolation": "Complete isolation between worktrees",
                "data_separation": "No cross-contamination between branches",
                "network_isolation": "Worktrees use separate Docker networks",
                "cleanup": "Remove worktrees when done to clean up data"
            }
        }
