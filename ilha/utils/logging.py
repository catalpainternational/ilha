"""
Logging and output utilities for ilha CLI.

This module provides colored output and logging functionality that matches
the bash script's output format.
"""

import time
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Initialize console for colored output
console = Console()
# Diagnostics ([INFO]/[WARNING]/[ERROR]/[SUCCESS]/[PHASE]/[TEST]) go to
# stderr so stdout stays machine-readable: `--json` consumers parse
# stdout, and a single stray log line there breaks them.
diag_console = Console(stderr=True)

# Global verbose mode flag
_verbose_mode = False

# Global quiet mode flag - when True, suppress stdout output
_quiet_mode = False

# Color constants (matching bash script)
class Colors:
    RED = "red"
    GREEN = "green"
    YELLOW = "yellow"
    BLUE = "blue"
    PURPLE = "purple"
    CYAN = "cyan"

def set_verbose(enabled: bool) -> None:
    """Set verbose mode for logging output."""
    global _verbose_mode
    _verbose_mode = enabled

def set_quiet_mode(enabled: bool) -> None:
    """Set quiet mode - when enabled, suppress stdout output."""
    global _quiet_mode
    _quiet_mode = enabled

set_mcp_mode = set_quiet_mode  # backwards compat alias

def is_quiet_mode() -> bool:
    """Check if quiet mode is enabled."""
    return _quiet_mode

is_mcp_mode = is_quiet_mode  # backwards compat alias

def is_verbose() -> bool:
    """Check if verbose mode is enabled."""
    return _verbose_mode

def log_info(message: str) -> None:
    """Log an info message (only shown in verbose mode)."""
    if _verbose_mode:
        timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        diag_console.print(f"[{timestamp}] [{Colors.BLUE}][INFO][/{Colors.BLUE}] {message}")

def log_success(message: str) -> None:
    """Log a success message."""
    if not _quiet_mode:
        diag_console.print(f"[{Colors.GREEN}][SUCCESS][/{Colors.GREEN}] {message}")

def log_warning(message: str) -> None:
    """Log a warning message (only shown in verbose mode)."""
    if _verbose_mode:
        timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        diag_console.print(f"[{timestamp}] [{Colors.YELLOW}][WARNING][/{Colors.YELLOW}] {message}")

def log_error(message: str) -> None:
    """Log an error message."""
    if not _quiet_mode:
        diag_console.print(f"[{Colors.RED}][ERROR][/{Colors.RED}] {message}")

def emit_error(message: str, json: bool) -> None:
    """Emit an error to JSON output or stderr depending on mode.

    Args:
        message: Error message to emit
        json: If True, emit to JSON output; if False, use log_error()
    """
    if json:
        # Imported lazily because json_output imports this module for plain logging.
        from .json_output import JSONOutput

        JSONOutput.print_error(message)
    else:
        log_error(message)

def log_phase(message: str) -> None:
    """Log a phase message."""
    if not _quiet_mode:
        diag_console.print(f"[{Colors.PURPLE}][PHASE][/{Colors.PURPLE}] {message}")

def log_test(message: str) -> None:
    """Log a test message."""
    if not _quiet_mode:
        diag_console.print(f"[{Colors.CYAN}][TEST][/{Colors.CYAN}] {message}")

def print_plain(message: str) -> None:
    """Print plain text without any prefix."""
    if not _quiet_mode:
        console.print(message)

def show_version() -> None:
    """Show version information."""
    from ilha.config.settings import VERSION, AUTHOR
    
    version_text = f"""ilha v{VERSION}
{AUTHOR}

Git Worktrees for Isolated Development Environments"""
    
    console.print(Panel(version_text, title="ilha", border_style=Colors.BLUE))

def show_help() -> None:
    """Show help information."""
    help_text = """ilha: Git Worktrees for Isolated Development Environments

Create isolated development environments using Git worktrees with Docker Compose.
Each worktree gets its own database, Redis, media storage, and unique URL.

USAGE:
    ilha <worktree_name> up|down
    ilha <command> [options]

COMMANDS:
    start                    Start global Caddy proxy container
    stop                     Stop global Caddy proxy container
    create <branch-name>     Create worktree for specified branch
    <worktree> up            Start worktree environment
    <worktree> down          Stop worktree environment
    delete <branch-name>     Delete worktree and branch completely
    remove <branch-name>     Remove worktree but keep git branch
    list                     List all active worktrees
    prune                    Remove prunable worktree references
    volumes                  Manage Docker volumes for worktrees
    packages                 Manage environment packages
    droplet                  Manage DigitalOcean droplets
    domains                  Manage DNS domains and records
    setup                    Initialize ilha for project
    completion               Manage shell completion
    --version, -v            Show version information
    help                     Show this help message

EXAMPLES:
    # Start global Caddy proxy
    ilha start
    
    # Create and start a worktree
    ilha create feature-auth
    ilha feature-auth up
    
    # Access environment (URL format: http://project-branch.localhost)
    open http://myproject-feature-auth.localhost
    
    # Stop and remove worktree (keeps git branch)
    ilha feature-auth down
    ilha remove feature-auth
    
    # Or delete worktree completely (removes git branch)
    ilha delete feature-auth
    
    # Force remove worktree with unmerged changes
    ilha remove feature-auth --force
    
    # Clean up prunable worktrees
    ilha prune
    
    # Stop global Caddy
    ilha stop

VOLUME MANAGEMENT:
    ilha volumes list                    List all worktree volumes
    ilha volumes size                    Show volume sizes
    ilha volumes backup <branch-name>    Backup worktree volumes
    ilha volumes restore <branch-name> <backup-file.tar>
                                                Restore worktree volumes
    ilha volumes clean <branch-name>     Clean up worktree volumes

For more information, use: ilha <command> --help"""
    
    console.print(Panel(help_text, title="ilha Help", border_style=Colors.BLUE))

def show_progress(message: str) -> Progress:
    """Show a progress indicator for long-running operations."""
    return Progress(
        SpinnerColumn(),
        TextColumn(f"[{Colors.BLUE}]{message}[/{Colors.BLUE}]"),
        console=console,
        transient=True
    )

def format_elapsed_time(seconds: float) -> str:
    """Format elapsed time in seconds into a readable string.
    
    Args:
        seconds: Elapsed time in seconds
        
    Returns:
        Formatted string like "2m 30s" or "1h 5m 20s"
    """
    if seconds < 60:
        return f"{int(seconds)}s"
    
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    
    if minutes < 60:
        return f"{minutes}m {secs}s"
    
    hours = int(minutes // 60)
    mins = int(minutes % 60)
    return f"{hours}h {mins}m {secs}s"

def error_exit(message: str, exit_code: int = 1) -> None:
    """Log an error and exit."""
    log_error(message)
    raise SystemExit(exit_code)


def fail_prerequisite(
    summary: str,
    hints: Optional[List[str]] = None,
    exit_code: int = 1,
) -> None:
    """Print a clear prerequisite failure and optional hint lines, then exit.

    Prefer this over ``error_exit`` when users need concrete next steps.
    """
    log_error(summary)
    if hints and not _quiet_mode:
        for line in hints:
            print_plain(f"  • {line}")
    raise SystemExit(exit_code)


def log_step(message: str) -> None:
    """Log a step message (always shown in non-quiet mode).

    Use this for major operation milestones that the user should see
    regardless of verbose mode.
    """
    if not _quiet_mode:
        diag_console.print(f"  [{Colors.BLUE}]●[/{Colors.BLUE}] {message}...", highlight=False)


# ──────────────────────────────────────────────────────────────────
# Option 2: Multi-stage progress tracker
# ──────────────────────────────────────────────────────────────────

class StageTracker:
    """Track and display multi-stage operation progress with timing.

    Usage::

        with StageTracker("Deploying feature-auth → 1.2.3.4") as tracker:
            with tracker.stage("Preflight checks"):
                run_preflight()
            # parallel stages — record results manually
            tracker.record("DNS configuration", elapsed=4.1)
            tracker.record("Server preparation", elapsed=72.3)
    """

    def __init__(self, title: str):
        self._title = title
        self._stages: List[Tuple[str, str, float]] = []  # (name, status, duration)
        self._start_time: Optional[float] = None

    def __enter__(self):
        self._start_time = time.time()
        if not _quiet_mode:
            console.print(f"\n[bold]{self._title}[/bold]")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    @contextmanager
    def stage(self, name: str):
        """Context manager for a sequential stage with spinner."""
        started = time.time()
        if _quiet_mode:
            yield
            elapsed = time.time() - started
            self._stages.append((name, "done", elapsed))
            return

        status = console.status(f"  [blue]●[/blue] {name}...")
        status.start()
        try:
            yield
        except Exception:
            status.stop()
            elapsed = time.time() - started
            self._stages.append((name, "failed", elapsed))
            console.print(
                f"  [{Colors.RED}]✗[/{Colors.RED}] {name}"
                f"  [dim]{format_elapsed_time(elapsed)}[/dim]",
                highlight=False,
            )
            raise
        else:
            status.stop()
            elapsed = time.time() - started
            self._stages.append((name, "done", elapsed))
            console.print(
                f"  [{Colors.GREEN}]✓[/{Colors.GREEN}] {name}"
                f"  [dim]{format_elapsed_time(elapsed)}[/dim]",
                highlight=False,
            )

    def record(self, name: str, elapsed: float, status: str = "done") -> None:
        """Record a completed stage (for parallel stages managed externally)."""
        self._stages.append((name, status, elapsed))
        if _quiet_mode:
            return
        if status == "done":
            console.print(
                f"  [{Colors.GREEN}]✓[/{Colors.GREEN}] {name}"
                f"  [dim]{format_elapsed_time(elapsed)}[/dim]",
                highlight=False,
            )
        elif status == "failed":
            console.print(
                f"  [{Colors.RED}]✗[/{Colors.RED}] {name}"
                f"  [dim]{format_elapsed_time(elapsed)}[/dim]",
                highlight=False,
            )
        elif status == "skipped":
            console.print(f"  [dim]–[/dim] [dim]{name} (skipped)[/dim]", highlight=False)

    @property
    def total_elapsed(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.time() - self._start_time

    @property
    def stages(self) -> List[Tuple[str, str, float]]:
        return list(self._stages)


# ──────────────────────────────────────────────────────────────────
# Option 3: Post-operation deploy report
# ──────────────────────────────────────────────────────────────────

_STAGE_DISPLAY_NAMES = {
    "preflight": "Preflight",
    "assemble_prepare_dns": "Server prep + assembly",
    "disk_check": "Disk check",
    "volume_backup": "Volume backup",
    "transfer": "Code sync",
    "remote_prepare": "Remote services",
    "app_start": "App start",
    "smoke_checks": "Smoke checks",
}


def render_deploy_report(report: Dict[str, Any]) -> None:
    """Render a structured deploy report table after a deploy operation."""
    if _quiet_mode:
        return

    stage_timings: Dict[str, float] = report.get("stage_timings", {})
    error_info: Optional[Dict[str, Any]] = report.get("error")
    success: bool = report.get("success", False)

    if not stage_timings and not error_info:
        return

    table = Table(border_style="dim", show_header=True, pad_edge=False, box=None)
    table.add_column("Stage", min_width=28)
    table.add_column("Duration", justify="right", min_width=8)
    table.add_column("Status", min_width=8)

    failed_stage_key = error_info.get("stage") if error_info else None
    total_seconds = 0.0

    for key, duration in stage_timings.items():
        name = _STAGE_DISPLAY_NAMES.get(key, key)
        total_seconds += duration
        if key == failed_stage_key:
            table.add_row(
                f"[red]{name}[/red]",
                format_elapsed_time(duration),
                "[red]✗ failed[/red]",
            )
        else:
            table.add_row(name, format_elapsed_time(duration), "[green]✓[/green]")

    # If the failed stage has no timing yet, add it separately
    if failed_stage_key and failed_stage_key not in stage_timings:
        name = _STAGE_DISPLAY_NAMES.get(failed_stage_key, failed_stage_key)
        table.add_row(f"[red]{name}[/red]", "", "[red]✗ failed[/red]")

    console.print()
    title_style = "[bold green]" if success else "[bold red]"
    title_label = "Deploy Report" if success else "Deploy Report (failed)"
    console.print(f"  {title_style}{title_label}[/]")
    console.print()
    console.print(table)
    if total_seconds > 0:
        console.print(f"\n  [bold]Total: {format_elapsed_time(total_seconds)}[/bold]")

    # Disk usage warning
    disk_check = report.get("disk_check")
    if disk_check:
        usage = disk_check.get("usage_percent") or disk_check.get("app_usage_percent")
        if usage and usage > 70:
            console.print(f"  [yellow]⚠ Disk usage on server: {usage}%[/yellow]")

    # URL on success
    url = report.get("url")
    if url and success:
        console.print(f"\n  [bold green]🌐 {url}[/bold green]")

    notes = report.get("notes") or []
    if notes and isinstance(notes, list):
        console.print("\n  [bold]Notes[/bold]")
        for note in notes:
            console.print(f"    • {note}")

    vb = report.get("volume_backup_path")
    if vb:
        console.print(f"\n  [dim]Volume backup path: {vb}[/dim]")

    # Suggested actions on failure
    if error_info:
        root_cause = error_info.get("root_cause")
        if root_cause:
            console.print(f"\n  [red]Cause:[/red] {root_cause}")
        actions = error_info.get("suggested_actions", [])
        if actions:
            console.print(f"  [yellow]Suggested actions:[/yellow]")
            for action in actions:
                console.print(f"    • {action}")
    console.print()
