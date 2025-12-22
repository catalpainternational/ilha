"""
Main MCP server implementation for ilha.

This module provides the core MCP server that exposes ilha functionality
through the Model Context Protocol, enabling AI assistants to manage
isolated development environments.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequest,
    CallToolResult,
    ListResourcesRequest,
    ListResourcesResult,
    ListToolsRequest,
    ListToolsResult,
    ReadResourceRequest,
    ReadResourceResult,
    Resource,
    Tool,
)

from .tools.worktree_tools import WorktreeTools
from .tools.volume_tools import VolumeTools
from .tools.caddy_tools import CaddyTools
from .tools.package_tools import PackageTools
from .tools.push_tools import PushTools
from .resources.worktree_resources import WorktreeResources
from .resources.documentation import ilhaDocumentation
from .config import MCPConfig


# Global server instance
server = Server("ilha-mcp")

# No global config - create per tool call
worktree_tools = None
volume_tools = None
caddy_tools = None
worktree_resources = None
documentation = ilhaDocumentation()


def get_workspace_from_context() -> Path:
    """Detect workspace directory from MCP context or environment."""
    # Try environment variables that IDEs like Cursor might set
    for env_var in ["CURSOR_WORKSPACE", "VSCODE_WORKSPACE", "WORKSPACE_DIR", "PROJECT_ROOT"]:
        if workspace := os.getenv(env_var):
            return Path(workspace)
    
    # Fallback to current working directory
    return Path.cwd()


@server.list_tools()
async def list_tools() -> List[Tool]:
    """List available MCP tools."""
    return [
        # Worktree management
        Tool(
            name="create_worktree",
            description="Create an isolated development environment for a Git branch using ilha. ilha combines Git worktrees (separate working directories) with Docker Compose (isolated containers) and Caddy proxy (dynamic routing). Each worktree gets its own database, Redis, media storage, and unique URL. This is the first step - after creation, use start_worktree to launch the environment.\n\nIMPORTANT: Always provide 'working_directory' parameter pointing to the target project directory (e.g., '/Users/ders/kenali/blank'). This tells ilha which project to operate on. The MCP server can run from any location but needs to know the target project path.",
            inputSchema={
                "type": "object",
                "properties": {
                    "branch_name": {
                        "type": "string",
                        "description": "Name of the Git branch to create isolated environment for (e.g., 'feature-auth', 'bugfix-payment')"
                    },
                    "working_directory": {
                        "type": "string",
                        "description": "REQUIRED in practice: Absolute path to the project directory where ilha should operate. Use the Cursor workspace path or your current project directory. Example: '/Users/ders/kenali/blank'",
                        "default": None
                    }
                },
                "required": ["branch_name"]
            },
            examples=[
                {
                    "input": {"branch_name": "feature-auth", "working_directory": "/Users/ders/kenali/blank"},
                    "output": "Creates worktree at /Users/ders/kenali/blank/.ilha/worktrees/feature-auth/ with isolated containers",
                    "next_step": "Use start_worktree to launch containers and access at http://blank-feature-auth.localhost"
                }
            ],
            workflow_context="First step in ilha workflow - creates isolated environment",
            related_tools=["start_worktree", "get_worktree_info", "list_worktrees"]
        ),
        Tool(
            name="start_worktree",
            description="Start the isolated Docker environment for a worktree. This launches all containers (database, Redis, web app) with branch-specific volumes. After starting, the environment is accessible via the unique subdomain. Each worktree runs independently with isolated data.\n\nIMPORTANT: Always provide 'working_directory' parameter pointing to the target project directory. This ensures the MCP server operates on the correct project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "branch_name": {
                        "type": "string",
                        "description": "Name of the branch/worktree to start (e.g., 'feature-auth')"
                    },
                    "working_directory": {
                        "type": "string",
                        "description": "REQUIRED in practice: Absolute path to the project directory where ilha should operate. Use the Cursor workspace path or your current project directory. Example: '/Users/ders/kenali/blank'",
                        "default": None
                    }
                },
                "required": ["branch_name"]
            },
            examples=[
                {
                    "input": {"branch_name": "feature-auth", "working_directory": "/Users/ders/kenali/blank"},
                    "output": "Starts containers and makes environment accessible at http://blank-feature-auth.localhost",
                    "next_step": "Access your environment at the provided URL"
                }
            ],
            workflow_context="Second step in ilha workflow - launches isolated environment",
            related_tools=["get_worktree_info", "stop_worktree", "list_worktrees"]
        ),
        Tool(
            name="stop_worktree",
            description="Stop the Docker containers for a worktree while preserving all data in branch-specific volumes. The worktree directory and volumes remain intact for later restart. This is useful for pausing development without losing data.\n\nIMPORTANT: Always provide 'working_directory' parameter pointing to the target project directory. This ensures the MCP server operates on the correct project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "branch_name": {
                        "type": "string",
                        "description": "Name of the branch/worktree to stop (e.g., 'feature-auth')"
                    },
                    "working_directory": {
                        "type": "string",
                        "description": "REQUIRED in practice: Absolute path to the project directory where ilha should operate. Use the Cursor workspace path or your current project directory. Example: '/Users/ders/kenali/blank'",
                        "default": None
                    }
                },
                "required": ["branch_name"]
            },
            examples=[
                {
                    "input": {"branch_name": "feature-auth", "working_directory": "/Users/ders/kenali/blank"},
                    "output": "Stops containers but preserves all data in volumes",
                    "next_step": "Use start_worktree to restart or remove_worktree to clean up"
                }
            ],
            workflow_context="Pause development while preserving data",
            related_tools=["start_worktree", "remove_worktree", "get_worktree_info"]
        ),
        Tool(
            name="remove_worktree",
            description="Remove the isolated environment and containers but preserve the Git branch. This is useful when you want to keep your code changes but clean up the development environment. The Git branch remains available for future use.\n\nIMPORTANT: Always provide 'working_directory' parameter pointing to the target project directory. This ensures the MCP server operates on the correct project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "branch_name": {
                        "type": "string",
                        "description": "Name of the branch/worktree to remove (e.g., 'feature-auth')"
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Force removal even with unmerged changes",
                        "default": False
                    },
                    "working_directory": {
                        "type": "string",
                        "description": "REQUIRED in practice: Absolute path to the project directory where ilha should operate. Use the Cursor workspace path or your current project directory. Example: '/Users/ders/kenali/blank'",
                        "default": None
                    }
                },
                "required": ["branch_name"]
            },
            examples=[
                {
                    "input": {"branch_name": "feature-auth", "working_directory": "/Users/ders/kenali/blank"},
                    "output": "Removes environment but keeps Git branch",
                    "next_step": "Use create_worktree to recreate environment or delete_worktree to remove branch too"
                }
            ],
            workflow_context="Clean up environment while preserving Git branch",
            related_tools=["create_worktree", "delete_worktree", "list_worktrees"]
        ),
        Tool(
            name="delete_worktree",
            description="Completely remove the isolated environment, containers, volumes, and Git branch. This is a destructive operation that cannot be undone. Use this when you want to completely clean up a feature branch and its environment.\n\nIMPORTANT: Always provide 'working_directory' parameter pointing to the target project directory. This ensures the MCP server operates on the correct project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "branch_name": {
                        "type": "string",
                        "description": "Name of the branch/worktree to delete (e.g., 'feature-auth')"
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Force deletion even with unmerged changes",
                        "default": False
                    },
                    "working_directory": {
                        "type": "string",
                        "description": "REQUIRED in practice: Absolute path to the project directory where ilha should operate. Use the Cursor workspace path or your current project directory. Example: '/Users/ders/kenali/blank'",
                        "default": None
                    }
                },
                "required": ["branch_name"]
            },
            examples=[
                {
                    "input": {"branch_name": "feature-auth", "working_directory": "/Users/ders/kenali/blank"},
                    "output": "Completely removes environment, containers, volumes, and Git branch",
                    "next_step": "Create new branch and environment if needed"
                }
            ],
            workflow_context="Complete cleanup - removes everything including Git branch",
            related_tools=["create_worktree", "remove_worktree", "list_worktrees"]
        ),
        Tool(
            name="list_worktrees",
            description="List all active isolated development environments with their current status, URLs, and container information. Shows which worktrees are running, stopped, or have issues. Essential for understanding your current development setup.\n\nIMPORTANT: Always provide 'working_directory' parameter pointing to the target project directory. This ensures the MCP server operates on the correct project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "working_directory": {
                        "type": "string",
                        "description": "REQUIRED in practice: Absolute path to the project directory where ilha should operate. Use the Cursor workspace path or your current project directory. Example: '/Users/ders/kenali/blank'",
                        "default": None
                    }
                }
            },
            examples=[
                {
                    "input": {"working_directory": "/Users/ders/kenali/blank"},
                    "output": "Shows all worktrees with status, URLs, and container counts",
                    "next_step": "Use get_worktree_info for detailed status of specific worktrees"
                }
            ],
            workflow_context="Discovery and status overview",
            related_tools=["get_worktree_info", "create_worktree", "start_worktree"]
        ),
        Tool(
            name="get_worktree_info",
            description="Get detailed information about a specific isolated development environment including container status, volumes, access URLs, and project context. Essential for understanding how to access and work with a specific worktree.\n\nIMPORTANT: Always provide 'working_directory' parameter pointing to the target project directory. This ensures the MCP server operates on the correct project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "branch_name": {
                        "type": "string",
                        "description": "Name of the branch/worktree to get info for (e.g., 'feature-auth')"
                    },
                    "working_directory": {
                        "type": "string",
                        "description": "REQUIRED in practice: Absolute path to the project directory where ilha should operate. Use the Cursor workspace path or your current project directory. Example: '/Users/ders/kenali/blank'",
                        "default": None
                    }
                },
                "required": ["branch_name"]
            },
            examples=[
                {
                    "input": {"branch_name": "feature-auth", "working_directory": "/Users/ders/kenali/blank"},
                    "output": "Detailed status including URL, containers, volumes, and access information",
                    "next_step": "Access at the provided URL or use start/stop_worktree as needed"
                }
            ],
            workflow_context="Detailed inspection of specific environment",
            related_tools=["start_worktree", "stop_worktree", "list_worktrees"]
        ),
        # Volume management
        Tool(
            name="list_volumes",
            description="List all worktree volumes with branch mapping and naming explanation.\n\nIMPORTANT: Always provide 'working_directory' parameter pointing to the target project directory. This ensures the MCP server operates on the correct project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "working_directory": {
                        "type": "string",
                        "description": "REQUIRED in practice: Absolute path to the project directory where ilha should operate. Use the Cursor workspace path or your current project directory. Example: '/Users/ders/kenali/blank'",
                        "default": None
                    }
                }
            }
        ),
        Tool(
            name="get_volume_sizes",
            description="Get sizes of all worktree volumes with detailed breakdown.\n\nIMPORTANT: Always provide 'working_directory' parameter pointing to the target project directory. This ensures the MCP server operates on the correct project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "working_directory": {
                        "type": "string",
                        "description": "REQUIRED in practice: Absolute path to the project directory where ilha should operate. Use the Cursor workspace path or your current project directory. Example: '/Users/ders/kenali/blank'",
                        "default": None
                    }
                }
            }
        ),
        Tool(
            name="backup_volumes",
            description="Backup volumes for a specific worktree with branch-specific data preservation.\n\nIMPORTANT: Always provide 'working_directory' parameter pointing to the target project directory. This ensures the MCP server operates on the correct project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "branch_name": {
                        "type": "string",
                        "description": "Name of the branch/worktree to backup"
                    },
                    "backup_dir": {
                        "type": "string",
                        "description": "Directory to store backup (optional)"
                    },
                    "working_directory": {
                        "type": "string",
                        "description": "REQUIRED in practice: Absolute path to the project directory where ilha should operate. Use the Cursor workspace path or your current project directory. Example: '/Users/ders/kenali/blank'",
                        "default": None
                    }
                },
                "required": ["branch_name"]
            }
        ),
        Tool(
            name="restore_volumes",
            description="Restore volumes for a specific worktree from backup with branch-specific data restoration.\n\nIMPORTANT: Always provide 'working_directory' parameter pointing to the target project directory. This ensures the MCP server operates on the correct project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "branch_name": {
                        "type": "string",
                        "description": "Name of the branch/worktree to restore"
                    },
                    "backup_file": {
                        "type": "string",
                        "description": "Path to backup file"
                    },
                    "working_directory": {
                        "type": "string",
                        "description": "REQUIRED in practice: Absolute path to the project directory where ilha should operate. Use the Cursor workspace path or your current project directory. Example: '/Users/ders/kenali/blank'",
                        "default": None
                    }
                },
                "required": ["branch_name", "backup_file"]
            }
        ),
        Tool(
            name="clean_volumes",
            description="Clean up volumes for a specific worktree with branch-specific data cleanup.\n\nIMPORTANT: Always provide 'working_directory' parameter pointing to the target project directory. This ensures the MCP server operates on the correct project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "branch_name": {
                        "type": "string",
                        "description": "Name of the branch/worktree to clean"
                    },
                    "working_directory": {
                        "type": "string",
                        "description": "REQUIRED in practice: Absolute path to the project directory where ilha should operate. Use the Cursor workspace path or your current project directory. Example: '/Users/ders/kenali/blank'",
                        "default": None
                    }
                },
                "required": ["branch_name"]
            }
        ),
        # Caddy proxy management
        Tool(
            name="start_proxy",
            description="Start the global Caddy proxy container for dynamic routing to worktrees.\n\nIMPORTANT: Always provide 'working_directory' parameter pointing to the target project directory. This ensures the MCP server operates on the correct project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "working_directory": {
                        "type": "string",
                        "description": "REQUIRED in practice: Absolute path to the project directory where ilha should operate. Use the Cursor workspace path or your current project directory. Example: '/Users/ders/kenali/blank'",
                        "default": None
                    }
                }
            }
        ),
        Tool(
            name="stop_proxy",
            description="Stop the global Caddy proxy container and disable dynamic routing.\n\nIMPORTANT: Always provide 'working_directory' parameter pointing to the target project directory. This ensures the MCP server operates on the correct project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "working_directory": {
                        "type": "string",
                        "description": "REQUIRED in practice: Absolute path to the project directory where ilha should operate. Use the Cursor workspace path or your current project directory. Example: '/Users/ders/kenali/blank'",
                        "default": None
                    }
                }
            }
        ),
        Tool(
            name="get_proxy_status",
            description="Get status of the global Caddy proxy with routing information.\n\nIMPORTANT: Always provide 'working_directory' parameter pointing to the target project directory. This ensures the MCP server operates on the correct project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "working_directory": {
                        "type": "string",
                        "description": "REQUIRED in practice: Absolute path to the project directory where ilha should operate. Use the Cursor workspace path or your current project directory. Example: '/Users/ders/kenali/blank'",
                        "default": None
                    }
                }
            }
        ),
        # Package management
        Tool(
            name="export_package",
            description="Export a worktree environment to shareable package (includes code by default)",
            inputSchema={
                "type": "object",
                "properties": {
                    "branch_name": {
                        "type": "string",
                        "description": "Name of the branch to export"
                    },
                    "output_dir": {
                        "type": "string",
                        "description": "Output directory for packages (optional, defaults to ./packages)"
                    },
                    "include_code": {
                        "type": "boolean",
                        "description": "Whether to include git archive of code (optional, defaults to true)"
                    },
                    "compressed": {
                        "type": "boolean",
                        "description": "Whether to compress package to .tar.gz (optional, defaults to true)"
                    },
                    "working_directory": {
                        "type": "string",
                        "description": "REQUIRED in practice: Absolute path to the project directory where ilha should operate. Use the Cursor workspace path or your current project directory. Example: '/Users/ders/kenali/blank'",
                        "default": None
                    }
                },
                "required": ["branch_name"]
            }
        ),
        Tool(
            name="import_package",
            description="Import environment from package with automatic standalone detection. If not in an existing ilha project, automatically creates a new project from the package. Can force standalone mode with explicit flag.\n\nIMPORTANT: For standalone imports, the package must include code (exported with --include-code).",
            inputSchema={
                "type": "object",
                "properties": {
                    "package_file": {
                        "type": "string",
                        "description": "Path to the package file"
                    },
                    "target_branch": {
                        "type": "string",
                        "description": "Target branch name (for normal mode, optional, defaults to package branch name)"
                    },
                    "restore_data": {
                        "type": "boolean",
                        "description": "Whether to restore volume data (optional, defaults to true)"
                    },
                    "standalone": {
                        "type": "boolean",
                        "description": "Force standalone mode to create new project (optional, None = auto-detect based on current directory)"
                    },
                    "target_directory": {
                        "type": "string",
                        "description": "Target directory for standalone import (optional, defaults to {project_name}-standalone)"
                    },
                    "ip": {
                        "type": "string",
                        "description": "IP override for HTTP-only deployments (optional)"
                    },
                    "domain": {
                        "type": "string",
                        "description": "Domain override (subdomain.domain.tld) for production/staging deployments (optional)"
                    },
                    "working_directory": {
                        "type": "string",
                        "description": "Working directory for normal mode import. Not used in standalone mode. Example: '/Users/ders/kenali/blank'",
                        "default": None
                    }
                },
                "required": ["package_file"]
            }
        ),
        Tool(
            name="list_packages",
            description="List available packages",
            inputSchema={
                "type": "object",
                "properties": {
                    "package_dir": {
                        "type": "string",
                        "description": "Package directory to search (optional, defaults to ./packages)"
                    },
                    "working_directory": {
                        "type": "string",
                        "description": "REQUIRED in practice: Absolute path to the project directory where ilha should operate. Use the Cursor workspace path or your current project directory. Example: '/Users/ders/kenali/blank'",
                        "default": None
                    }
                }
            }
        ),
        Tool(
            name="validate_package",
            description="Validate package integrity",
            inputSchema={
                "type": "object",
                "properties": {
                    "package_file": {
                        "type": "string",
                        "description": "Path to the package file"
                    },
                    "working_directory": {
                        "type": "string",
                        "description": "REQUIRED in practice: Absolute path to the project directory where ilha should operate. Use the Cursor workspace path or your current project directory. Example: '/Users/ders/kenali/blank'",
                        "default": None
                    }
                },
                "required": ["package_file"]
            }
        ),
        # Push/deployment tools
        Tool(
            name="push_package",
            description="Push ilha package to remote server via SCP. Exports a complete ilha environment package and transfers it to a remote server for deployment. If branch_name is not provided, auto-detects from current working directory.",
            inputSchema={
                "type": "object",
                "properties": {
                    "branch_name": {
                        "type": "string",
                        "description": "Name of the branch to push (optional, auto-detects if not provided)"
                    },
                    "scp_target": {
                        "type": "string",
                        "description": "SCP target in format username@server:path (required)"
                    },
                    "output_dir": {
                        "type": "string",
                        "description": "Temporary package location (optional, defaults to ./packages)"
                    },
                    "keep_package": {
                        "type": "boolean",
                        "description": "Don't delete package after successful push (optional, defaults to false)"
                    },
                    "working_directory": {
                        "type": "string",
                        "description": "REQUIRED in practice: Absolute path to the project directory where ilha should operate. Use the Cursor workspace path or your current project directory. Example: '/Users/ders/kenali/blank'",
                        "default": None
                    }
                },
                "required": ["scp_target"]
            }
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> List[dict]:
    """Handle tool calls."""
    try:
        # Extract working directory from arguments or use current directory
        working_dir = arguments.get("working_directory")
        if working_dir:
            working_dir = Path(working_dir)
        else:
            working_dir = Path.cwd()
        
        # Create fresh config for this call
        config = MCPConfig(working_directory=working_dir)
        
        # Create tool instances with fresh config
        worktree_tools = WorktreeTools(config)
        volume_tools = VolumeTools(config)
        caddy_tools = CaddyTools(config)
        package_tools = PackageTools()
        push_tools = PushTools(config)
        
        # Route to appropriate tool handler
        if name == "create_worktree":
            result = await worktree_tools.create_worktree(arguments)
        elif name == "start_worktree":
            result = await worktree_tools.start_worktree(arguments)
        elif name == "stop_worktree":
            result = await worktree_tools.stop_worktree(arguments)
        elif name == "remove_worktree":
            result = await worktree_tools.remove_worktree(arguments)
        elif name == "delete_worktree":
            result = await worktree_tools.delete_worktree(arguments)
        elif name == "list_worktrees":
            result = await worktree_tools.list_worktrees(arguments)
        elif name == "get_worktree_info":
            result = await worktree_tools.get_worktree_info(arguments)
        elif name == "list_volumes":
            result = await volume_tools.list_volumes(arguments)
        elif name == "get_volume_sizes":
            result = await volume_tools.get_volume_sizes(arguments)
        elif name == "backup_volumes":
            result = await volume_tools.backup_volumes(arguments)
        elif name == "restore_volumes":
            result = await volume_tools.restore_volumes(arguments)
        elif name == "clean_volumes":
            result = await volume_tools.clean_volumes(arguments)
        elif name == "start_proxy":
            result = await caddy_tools.start_proxy(arguments)
        elif name == "stop_proxy":
            result = await caddy_tools.stop_proxy(arguments)
        elif name == "get_proxy_status":
            result = await caddy_tools.get_proxy_status(arguments)
        elif name == "export_package":
            result = await package_tools.export_package(arguments)
        elif name == "import_package":
            result = await package_tools.import_package(arguments)
        elif name == "list_packages":
            result = await package_tools.list_packages(arguments)
        elif name == "validate_package":
            result = await package_tools.validate_package(arguments)
        elif name == "push_package":
            result = await push_tools.push_package(arguments)
        else:
            result = {"error": f"Unknown tool: {name}"}
        
        return [{
            "type": "text",
            "text": json.dumps(result, indent=2)
        }]
    except Exception as e:
        return [{
            "type": "text",
            "text": json.dumps({"error": str(e)}, indent=2)
        }]


@server.list_resources()
async def list_resources() -> List[Resource]:
    """List available MCP resources."""
    return [
        # Static documentation resources
        Resource(
            uri="ilha://concept",
            name="ilha Concept",
            description="Comprehensive explanation of what ilha is and how it works",
            mimeType="application/json"
        ),
        Resource(
            uri="ilha://architecture",
            name="Technical Architecture",
            description="Detailed technical architecture of Git worktrees, Docker, and Caddy integration",
            mimeType="application/json"
        ),
        Resource(
            uri="ilha://workflows",
            name="Workflow Patterns",
            description="Common ilha usage patterns with step-by-step examples",
            mimeType="application/json"
        ),
        Resource(
            uri="ilha://terminology",
            name="ilha Glossary",
            description="Glossary of ilha-specific terms and concepts",
            mimeType="application/json"
        ),
        Resource(
            uri="ilha://url-patterns",
            name="URL Patterns",
            description="How URLs are constructed and routing works in ilha",
            mimeType="application/json"
        ),
        # Dynamic project resources
        Resource(
            uri="ilha://project",
            name="Project Context",
            description="Current project context and configuration with ilha concept",
            mimeType="application/json"
        ),
        Resource(
            uri="ilha://worktrees",
            name="Active Worktrees",
            description="List of all active worktrees with their status and URLs",
            mimeType="application/json"
        ),
        Resource(
            uri="ilha://volumes",
            name="Worktree Volumes",
            description="List of all worktree volumes with branch mapping and naming explanation",
            mimeType="application/json"
        ),
        Resource(
            uri="ilha://proxy",
            name="Caddy Proxy Status",
            description="Status of the global Caddy proxy container with routing information",
            mimeType="application/json"
        ),
        Resource(
            uri="ilha://state",
            name="Complete State",
            description="Complete current state summary with all ilha information",
            mimeType="application/json"
        ),
        Resource(
            uri="ilha://best-practices",
            name="Best Practices",
            description="ilha best practices, performance tips, and troubleshooting guide",
            mimeType="application/json"
        )
    ]


@server.read_resource()
async def read_resource(uri: str) -> str:
    """Read MCP resources."""
    try:
        # Static documentation resources
        if uri == "ilha://concept":
            data = documentation.get_concept()
        elif uri == "ilha://architecture":
            data = documentation.get_architecture()
        elif uri == "ilha://workflows":
            data = documentation.get_workflow_patterns()
        elif uri == "ilha://terminology":
            data = documentation.get_terminology()
        elif uri == "ilha://url-patterns":
            data = documentation.get_url_patterns()
        elif uri == "ilha://best-practices":
            data = documentation.get_best_practices()
        # Dynamic project resources - create resources per-request with working directory
        else:
            # Get working directory from context or use current directory
            working_dir = get_workspace_from_context()
            config = MCPConfig(working_directory=working_dir)
            worktree_resources = WorktreeResources(config)
            
            if uri == "ilha://project":
                data = await worktree_resources.get_project_context()
            elif uri == "ilha://worktrees":
                data = await worktree_resources.get_worktrees_with_urls()
            elif uri == "ilha://volumes":
                data = await worktree_resources.get_volumes_with_branches()
            elif uri == "ilha://proxy":
                data = await worktree_resources.get_proxy_with_routes()
            elif uri == "ilha://state":
                data = await worktree_resources.get_complete_state()
            else:
                data = {"error": f"Unknown resource: {uri}"}
        
        return json.dumps(data, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


class ilhaMCPServer:
    """
    Backwards-compatible wrapper exposing the legacy MCP server class API.

    The refactored module relies on module-level handlers registered against a
    shared ``Server`` instance, but older tests (and some integrations) still
    import ``ilhaMCPServer``. This lightweight adapter recreates the
    attributes those callers expect without altering the runtime wiring.
    """

    def __init__(self, config: Optional[MCPConfig] = None):
        self.config = config or MCPConfig()
        self.server = server

        # Historical attributes verified by tests/integrations.
        self.worktree_tools = WorktreeTools(self.config)
        self.volume_tools = VolumeTools(self.config)
        self.caddy_tools = CaddyTools(self.config)
        self.package_tools = PackageTools()
        self.push_tools = PushTools(self.config)
        self.worktree_resources = WorktreeResources(self.config)

        # Provide easy access to the registration helpers for assertions.
        self._list_tools = getattr(self.server, "list_tools", None)
        self._list_resources = getattr(self.server, "list_resources", None)


async def main():
    """Main entry point for the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options()
        )


def cli_main():
    """CLI entry point for the MCP server."""
    asyncio.run(main())


if __name__ == "__main__":
    cli_main()