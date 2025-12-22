"""
CLI command registration entry points.
"""

from __future__ import annotations

from typing import Callable, List


def register_all_commands(cli) -> None:
    """Register every command group with the root CLI instance."""
    for register in _collect_registrars():
        register(cli)


def _collect_registrars() -> List[Callable]:
    from . import completion, compose, domains, droplets, packages, proxy, push, server_import, setup, volumes, worktrees

    return [
        proxy.register_commands,
        worktrees.register_commands,
        volumes.register_commands,
        droplets.register_commands,
        domains.register_commands,
        packages.register_commands,
        push.register_commands,
        server_import.register_commands,
        setup.register_commands,
        completion.register_commands,
        compose.register_commands,
    ]


