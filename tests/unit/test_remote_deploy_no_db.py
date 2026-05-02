"""Static-site deploys must run without a Postgres readiness wait.

These tests pin two contracts:
- The remote wrapper script exports empty ``DB_SERVICE`` / ``DEPS_SERVICES`` /
  ``WORKER_SERVICES`` for a static-site project.
- ``remote_deploy.py`` skips the dependency-up step and the database wait
  when those env vars are empty.
"""

from pathlib import Path

import pytest

from ilha.commands.push.slim_deploy_manager import SlimDeployManager


def _make_static_project(root: Path) -> None:
    (root / ".ilha").mkdir(parents=True, exist_ok=True)
    (root / ".ilha" / "docker-compose.overlay.yml").write_text(
        "services:\n  caddy:\n    image: caddy:2-alpine\n    ports: ['8080:80']\n"
    )


def test_wrapper_script_exports_empty_db_for_static_site(tmp_path: Path) -> None:
    _make_static_project(tmp_path)
    mgr = SlimDeployManager(project_root=tmp_path)

    script = mgr._compose_remote_deploy_wrapper_script(
        app_root="/srv/app",
        remote_tmp_dir="/tmp/ilha-session",
        remote_deploy_file="/tmp/ilha-session/remote_deploy.py",
        build=False,
    )

    # Static defaults should bubble through to env exports.
    assert "export WEB_SERVICE=caddy" in script
    assert "export WEB_PORT=80" in script
    assert "export HEALTH_PATH=/" in script
    # Empty quoted values for db / deps / workers — the script must still set
    # the variables so the remote side doesn't fall through to a default.
    assert "export DB_SERVICE=''" in script
    assert "export DEPS_SERVICES=''" in script
    assert "export WORKER_SERVICES=''" in script


def test_remote_deploy_script_skips_db_wait_when_unset() -> None:
    """remote_deploy.py source contract: it must branch on db_service and deps_services."""
    body = (
        Path(__file__).resolve().parents[2]
        / "ilha"
        / "scripts"
        / "remote_deploy.py"
    ).read_text()

    # The dependency_services block must be skippable.
    assert "if self.deps_services:" in body
    assert "No dependency services configured" in body

    # The db readiness wait must be skippable.
    assert "if self.db_service:" in body
    assert "No database service configured" in body
    # We must not call wait_for_postgres outside of the db_service branch.
    pre, sep, post = body.partition("if self.db_service:")
    assert "wait_for_postgres" not in pre, (
        "wait_for_postgres should only run inside the db_service branch"
    )
