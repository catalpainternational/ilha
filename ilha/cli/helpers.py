"""
Shared helpers and decorators for ilha CLI commands.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Optional

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


def add_secrets_option(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator to add ``--secrets <profile>`` for SOPS-backed env layers."""
    return click.option(
        "--secrets",
        "secrets_profile",
        default=None,
        help="SOPS secret profile name from .ilha/config.yml (decrypted at runtime)",
    )(func)


def add_json_option(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator to add a JSON output flag to a command."""
    return click.option(
        "--json",
        is_flag=True,
        default=False,
        help="Output as JSON format",
    )(func)


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

