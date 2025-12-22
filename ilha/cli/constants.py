"""
Constants used across ilha CLI command modules.
"""

RESERVED_COMMANDS = {
    "start-proxy",
    "stop-proxy",
    "start",
    "stop",
    "create",
    "delete",
    "remove",
    "remove-all",
    "delete-all",
    "list",
    "prune",
    "volumes",
    "setup",
    "help",
    "completion",
    "packages",
    "droplet",
    "domains",
    "-D",
    "-r",
}

COMPOSE_PASSTHROUGH_COMMANDS = {
    "exec",
    "logs",
    "ps",
    "run",
    "build",
    "pull",
    "restart",
    "up",
    "down",
    "config",
    "images",
    "port",
    "top",
    "events",
    "kill",
    "pause",
    "unpause",
    "scale",
}

ALIAS_FLAGS = {
    "-D": "delete",
    "-r": "remove",
}


