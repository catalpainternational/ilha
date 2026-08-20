"""CLI-level tests for the compose passthrough.

The bug these guard against: when an inner command (e.g. a Django
management command) exits non-zero, ilha used to mislabel that as
"Failed to run docker compose command for worktree '<name>'", obscuring
the real error from the user. Inner exit codes must propagate verbatim,
and ilha-level errors must surface their specific preflight messages.
"""

import pytest
from click.testing import CliRunner

from ilha.cli import cli
from ilha.exceptions import ilhaCommandError


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
def noop_prerequisites(monkeypatch):
    monkeypatch.setattr("ilha.cli.helpers.check_setup_or_prompt", lambda: None)
    monkeypatch.setattr("ilha.cli.helpers.check_prerequisites", lambda: None)


def test_inner_nonzero_exit_propagates_without_wrapper_message(monkeypatch, runner):
    """A non-zero inner exit must reach the parent shell verbatim with no
    "Failed to run docker compose command" wrapper message."""

    def fake_passthrough(self, branch_name, compose_args, **kwargs):
        return 7

    monkeypatch.setattr(
        "ilha.core.docker_manager.DockerManager.run_compose_passthrough",
        fake_passthrough,
    )
    monkeypatch.setattr(
        "ilha.core.docker_manager.DockerManager.__init__",
        lambda self: None,
    )

    result = runner.invoke(cli, ["ainaro", "exec", "web", "false"])

    assert result.exit_code == 7
    combined = result.output or ""
    assert "Failed to run docker compose command" not in combined


def test_inner_zero_exit_succeeds(monkeypatch, runner):
    monkeypatch.setattr(
        "ilha.core.docker_manager.DockerManager.run_compose_passthrough",
        lambda self, branch_name, compose_args, **kwargs: 0,
    )
    monkeypatch.setattr(
        "ilha.core.docker_manager.DockerManager.__init__",
        lambda self: None,
    )

    result = runner.invoke(cli, ["ainaro", "exec", "web", "true"])
    assert result.exit_code == 0


def test_preflight_error_surfaces_specific_message(monkeypatch, runner):
    """ilhaCommandError raised by run_compose_passthrough (e.g. missing
    compose file) must reach the user with its own message intact."""

    def raising(self, branch_name, compose_args, **kwargs):
        raise ilhaCommandError("docker-compose.yml not found in worktree: /x")

    monkeypatch.setattr(
        "ilha.core.docker_manager.DockerManager.run_compose_passthrough",
        raising,
    )
    monkeypatch.setattr(
        "ilha.core.docker_manager.DockerManager.__init__",
        lambda self: None,
    )

    result = runner.invoke(cli, ["ainaro", "exec", "web", "true"])

    assert result.exit_code != 0
    combined = result.output or ""
    assert "docker-compose.yml not found in worktree: /x" in combined
    assert "Failed to run docker compose command" not in combined
