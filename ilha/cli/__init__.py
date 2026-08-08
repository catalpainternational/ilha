"""
Click CLI framework for ilha CLI.
"""

from __future__ import annotations

import sys
from typing import List, Sequence

import click

from ilha.cli.constants import ALIAS_FLAGS, COMPOSE_PASSTHROUGH_COMMANDS, RESERVED_COMMANDS
from ilha.cli.helpers import verbose_callback
from ilha.cli_commands import register_all_commands
from ilha.commands.completion import CompletionManager
from ilha.commands.worktree import WorktreeManager
from ilha.utils.completion_helper import get_completion_for_context
from ilha.utils.logging import error_exit, log_error
from ilha.utils.validation import (
    check_prerequisites,
    check_prerequisites_no_git,
    check_setup_or_prompt,
)


class ilhaCLI(click.Group):
    """Custom Click group to support ilha command ergonomics."""

    def parse_args(self, ctx: click.Context, args: List[str]):  # type: ignore[override]
        if not args:
            return super().parse_args(ctx, args)

        alias = ALIAS_FLAGS.get(args[0])
        if alias:
            args = [alias] + args[1:]

        elif len(args) >= 2 and args[1] in {"up", "down"} and args[0] not in RESERVED_COMMANDS:
            worktree_name = args[0]
            command = args[1]
            remaining_args = args[2:] if len(args) > 2 else []
            new_args = [command, worktree_name] + remaining_args
            return super().parse_args(ctx, new_args)

        elif len(args) >= 2 and args[0] not in RESERVED_COMMANDS:
            if args[1] in COMPOSE_PASSTHROUGH_COMMANDS or args[1].startswith("-"):
                worktree_name = args[0]
                compose_args = args[1:]
                ctx.meta["compose"] = {
                    "worktree_name": worktree_name,
                    "compose_args": compose_args,
                }
                new_args = ["compose", worktree_name] + compose_args
                return super().parse_args(ctx, new_args)

        elif args[0] in {"up", "down"}:
            # Try to resolve worktree from current context
            resolved_branch = None
            
            # Lazy import to avoid circular dependencies
            from ..utils.path_utils import detect_execution_context, get_worktree_branch_name
            from ..core.git_manager import GitManager
            from ..config.settings import get_project_root
            
            # First, try to detect if we're in a worktree directory
            worktree_path, branch_name, is_worktree = detect_execution_context()
            if branch_name:
                resolved_branch = branch_name
            else:
                # Not in a worktree, try to use current git branch
                try:
                    project_root = get_project_root()
                    git_manager = GitManager(project_root=project_root, validate=False)
                    current_branch = git_manager.get_current_branch()
                    if current_branch:
                        # Validate that a worktree exists for this branch
                        if git_manager.validate_worktree_exists(current_branch):
                            resolved_branch = current_branch
                except Exception:
                    # If we can't determine the branch, we'll error below
                    pass
            
            if resolved_branch:
                # Rewrite args: command, branch_name, then remaining args
                # For "up", ensure "-d" is included if not already present
                remaining_args = args[1:] if len(args) > 1 else []
                
                if args[0] == "up":
                    # Ensure "-d" is present for "up" command (docker compose up -d)
                    if "-d" not in remaining_args and "--detach" not in remaining_args:
                        remaining_args = ["-d"] + remaining_args
                
                new_args = [args[0], resolved_branch] + remaining_args
                return super().parse_args(ctx, new_args)
            else:
                # Could not resolve worktree - provide helpful error
                error_exit(
                    f"Error: Could not determine worktree. Run from a worktree directory or use: "
                    f"ilha <worktree_name> {args[0]}"
                )

        return super().parse_args(ctx, args)


def _build_cli() -> click.Group:
    cli = ilhaCLI(
        help="""ilha: Git Worktrees for Isolated Development Environments

Create isolated development environments using Git worktrees with Docker Compose.
Each worktree gets its own database, Redis, media storage, and unique URL.

Usage:
    ilha <worktree_name> up|down
    ilha up|down  (from worktree directory or project root with current branch)

    ilha <command> [options]

Examples:
    ilha create feature-auth
    ilha feature-auth up
    ilha up  (from worktree directory or project root)
    ilha feature-auth down
    ilha down  (from worktree directory or project root)
    ilha remove feature-auth
    ilha list
"""
    )
    from ilha.config.settings import VERSION

    cli = click.version_option(version=VERSION, prog_name="ilha")(cli)
    cli = click.option(
        "--verbose",
        "-v",
        is_flag=True,
        default=False,
        help="Enable verbose output (show INFO and WARNING messages)",
        callback=verbose_callback,
        expose_value=False,
        is_eager=True,
    )(cli)

    register_all_commands(cli)
    return cli


cli = _build_cli()


def _resolve_command(path: Sequence[str]):
    command = cli
    for name in path:
        commands = getattr(command, "commands", None)
        if not commands:
            return None
        command = commands.get(name)
        if command is None:
            return None
    return getattr(command, "callback", None)


def _invoke_command(path: Sequence[str], *args, **kwargs):
    callback = _resolve_command(path)
    if callback is None:
        raise AttributeError(f"Command {' '.join(path)} is not available")
    return callback(*args, **kwargs)


def up(*args, **kwargs):
    """Backwards-compatible helper for tests relying on ilha.cli.up."""
    return _invoke_command(["up"], *args, **kwargs)


def _completion(*args, **kwargs):
    """Backwards-compatible helper exposing the hidden completion command."""
    return _invoke_command(["_completion"], *args, **kwargs)


def completion_install(*args, **kwargs):
    """Backwards-compatible helper for completion install command."""
    return _invoke_command(["completion", "install"], *args, **kwargs)


def completion_uninstall(*args, **kwargs):
    """Backwards-compatible helper for completion uninstall command."""
    return _invoke_command(["completion", "uninstall"], *args, **kwargs)


def completion_status(*args, **kwargs):
    """Backwards-compatible helper for completion status command."""
    return _invoke_command(["completion", "status"], *args, **kwargs)


def main():
    """Main entry point for the CLI."""
    try:
        cli()
    except KeyboardInterrupt:
        log_error("Operation cancelled by user")
        sys.exit(1)
    except Exception as exc:
        log_error(f"Unexpected error: {exc}")
        sys.exit(1)


__all__ = [
    "cli",
    "main",
    "up",
    "_completion",
    "completion_install",
    "completion_uninstall",
    "completion_status",
    "CompletionManager",
    "WorktreeManager",
    "get_completion_for_context",
    "check_prerequisites",
    "check_prerequisites_no_git",
    "check_setup_or_prompt",
]

