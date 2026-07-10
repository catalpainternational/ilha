"""Regression tests for lazy CLI construction.

``ilha.cli`` used to build the whole Click tree at import time
(``cli = _build_cli()``), which meant importing a command module before
``ilha.cli`` re-entered registration mid-flight and hit a circular import.
The build is now lazy; these tests pin that contract.
"""

import subprocess
import sys


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )


def test_import_command_module_before_cli_does_not_cycle():
    # The previously-failing order: a command module first, ilha.cli second.
    result = _run(
        "import ilha.cli_commands.droplets\n"
        "import ilha.cli\n"
        "print('ok')\n"
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_importing_ilha_cli_does_not_eagerly_load_command_modules():
    # Merely importing ilha.cli must not pull command modules into sys.modules.
    result = _run(
        "import sys, ilha.cli\n"
        "loaded = [m for m in sys.modules if m.startswith('ilha.cli_commands.')]\n"
        "print('LOADED:' + ','.join(sorted(loaded)))\n"
    )
    assert result.returncode == 0, result.stderr
    assert "LOADED:" in result.stdout
    # No concrete command submodule should be imported yet.
    loaded_line = [l for l in result.stdout.splitlines() if l.startswith("LOADED:")][0]
    assert loaded_line == "LOADED:", loaded_line


def test_cli_attribute_still_resolves_and_is_populated():
    import ilha.cli

    cli = ilha.cli.cli  # triggers the lazy build via __getattr__
    assert hasattr(cli, "commands")
    # A representative sample of registered commands is present.
    for name in ("create", "up", "down", "list"):
        assert name in cli.commands, f"{name} missing from CLI"


def test_get_cli_is_cached_singleton():
    import ilha.cli

    assert ilha.cli.get_cli() is ilha.cli.get_cli()
    assert ilha.cli.get_cli() is ilha.cli.cli
