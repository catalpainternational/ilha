"""
Shared helpers and decorators for ilha CLI commands.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Iterable, Optional

import click

from ilha.exceptions import ilhaCommandError
from ilha.utils.json_output import JSONOutput
from ilha.utils.logging import error_exit, set_verbose
from ilha.utils.validation import check_prerequisites, check_setup_or_prompt


def verbose_callback(_: click.Context, __: click.Option, value: bool) -> bool:
    """Callback used by --verbose option on the root CLI."""
    set_verbose(value)
    return value


def add_verbose_option(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator to add the shared verbose flag to a command."""
    return click.option(
        "--verbose",
        "-v",
        is_flag=True,
        default=False,
        help="Enable verbose output (show INFO and WARNING messages)",
        callback=verbose_callback,
        expose_value=False,
        is_eager=True,
    )(func)


def add_json_option(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator to add a JSON output flag to a command."""
    return click.option(
        "--json",
        is_flag=True,
        default=False,
        help="Output as JSON format",
    )(func)


ClickOption = tuple[tuple[str, ...], dict[str, Any]]


REMOTE_PUSH_OPTIONS: tuple[ClickOption, ...] = (
    (
        ("--output-dir",),
        {
            "type": click.Path(),
            "default": "./packages",
            "help": "Temporary package location (default: ./packages)",
        },
    ),
    (
        ("--keep-package",),
        {
            "is_flag": True,
            "default": False,
            "help": "Keep package file after successful push (default: delete after push)",
        },
    ),
    (
        ("--no-auto-import",),
        {
            "is_flag": True,
            "default": False,
            "help": "Skip automatic import and start on remote server after push (default: auto-import is enabled)",
        },
    ),
    (
        ("--prepare-server",),
        {
            "is_flag": True,
            "default": False,
            "help": "Check remote server for required dependencies before push",
        },
    ),
    (
        ("--domain",),
        {
            "help": "Domain override for remote import (subdomain.domain.tld). DNS A record will be automatically created if it does not exist.",
        },
    ),
    (
        ("--ip",),
        {
            "help": "IP override for remote import (HTTP-only, no TLS)",
        },
    ),
    (
        ("--dns-token",),
        {
            "help": "DigitalOcean API token (or use DIGITALOCEAN_API_TOKEN env var)",
        },
    ),
    (
        ("--skip-dns-check",),
        {
            "is_flag": True,
            "default": False,
            "help": "Skip DNS validation and management",
        },
    ),
    (
        ("--resume",),
        {
            "is_flag": True,
            "default": False,
            "help": "Resume a failed push operation by detecting what's already completed (skips export/transfer if package exists, skips server prep if already done)",
        },
    ),
    (
        ("--code-only",),
        {
            "is_flag": True,
            "default": False,
            "help": "[NOT IMPLEMENTED] Push code-only update to pre-existing server (uses stored push config from env.ilha if available)",
        },
    ),
    (
        ("--build",),
        {
            "is_flag": True,
            "default": False,
            "help": "Rebuild Docker images on the remote server after deployment",
        },
    ),
    (
        ("--containers",),
        {
            "help": "Comma-separated list of worktree.container patterns to push only specific containers and their volumes (e.g., feature-auth.db,feature-auth.redis)",
        },
    ),
    (
        ("--exclude-deps",),
        {
            "help": "Comma-separated list of service names to exclude from dependency resolution (e.g., db,redis). Useful when deploying workers that connect to remote services.",
        },
    ),
)


def add_click_options(options: Iterable[ClickOption]) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Apply a reusable sequence of Click options to a command."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        for param_decls, kwargs in reversed(tuple(options)):
            func = click.option(*param_decls, **kwargs)(func)
        return func

    return decorator


def add_remote_push_options(
    *,
    include_debug: bool = False,
    include_use_staging_certificates: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Add the shared remote push/deploy option set used by multiple commands."""

    options = list(REMOTE_PUSH_OPTIONS)
    if include_debug:
        options.append(
            (
                ("--debug",),
                {
                    "is_flag": True,
                    "default": False,
                    "help": "Enable DEBUG mode in production deployment (default: False for production)",
                },
            )
        )
    if include_use_staging_certificates:
        options.append(
            (
                ("--use-staging-certificates",),
                {
                    "is_flag": True,
                    "default": False,
                    "help": "Use Let's Encrypt staging certificates (doesn't count against rate limits)",
                },
            )
        )
    return add_click_options(options)


def wrap_click_errors(action: Optional[str] = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Convert unexpected command callback failures into Click-friendly errors."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except click.ClickException:
                raise
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                message = str(exc) or action or func.__name__.replace("_", " ")
                if action and str(exc):
                    message = f"{action}: {exc}"
                elif action:
                    message = action
                raise click.ClickException(message) from exc

        return wrapper

    return decorator


def handle_json_result(result: Any, json_enabled: bool) -> None:
    """Render structured results when JSON output is requested."""
    if not json_enabled:
        return
    if isinstance(result, dict):
        JSONOutput.print_json(result)
    elif isinstance(result, list):
        JSONOutput.print_json(result)
    elif isinstance(result, bool):
        JSONOutput.print_json(
            JSONOutput.success("Operation completed" if result else "Operation failed")
        )
    elif result is not None:
        JSONOutput.print_json(JSONOutput.success("Operation completed", {"result": result}))


def command_wrapper(
    *,
    require_setup: bool = True,
    require_prerequisites: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Decorator to provide consistent prerequisite handling and error reporting.

    The wrapped function must accept a ``json`` keyword argument.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            json_enabled = kwargs.get("json", False)
            try:
                if require_setup:
                    check_setup_or_prompt()
                if require_prerequisites:
                    check_prerequisites()
                result = func(*args, **kwargs)
                handle_json_result(result, json_enabled)
                return result
            except ilhaCommandError as exc:
                if json_enabled:
                    JSONOutput.print_error(
                        exc.message,
                        error_code=exc.error_code,
                        details=exc.details,
                        json_output=True,
                    )
                else:
                    error_exit(exc.message, exit_code=exc.exit_code)
            except Exception as exc:
                if json_enabled:
                    JSONOutput.print_error(str(exc), error_code="command_error", json_output=True)
                else:
                    raise

        return wrapper

    return decorator

