import re
from pathlib import Path

from ilha.commands.push import PushManager


def test_compose_remote_script_basic(tmp_path: Path):
    pm = PushManager(project_root=tmp_path)
    script = pm._compose_remote_script(
        remote_file="/var/ilha/packages/pkg.tar.gz",
        branch_name="feature-auth",
        domain=None,
        ip=None,
    )

    # Must use strict mode and resolve DTBIN
    assert "set -euo pipefail" in script
    assert "DTBIN=" in script

    # Must import and start without empty commands
    assert "packages import" in script
    assert "start-proxy" in script
    assert re.search(r"\$DTBIN\"?\s+\"?start-proxy", script) is not None

    # No empty cd or empty test brackets
    assert "cd  &&" not in script
    assert "[ -n  ]" not in script

    # Must bring up the branch
    assert '"$DTBIN" "$BRANCH_NAME" up -d' in script


def test_compose_remote_script_with_domain_and_ip(tmp_path: Path):
    pm = PushManager(project_root=tmp_path)
    # Domain case
    script_domain = pm._compose_remote_script(
        remote_file="/path/pkg.tar.gz",
        branch_name="feature-auth",
        domain="app.example.com",
        ip=None,
    )
    assert '--domain' in script_domain
    assert '"app.example.com"' in script_domain

    # IP case
    script_ip = pm._compose_remote_script(
        remote_file="/path/pkg.tar.gz",
        branch_name="feature-auth",
        domain=None,
        ip="203.0.113.10",
    )
    assert '--ip' in script_ip
    assert '"203.0.113.10"' in script_ip


def test_compose_remote_script_non_interactive_flag(tmp_path: Path):
    pm = PushManager(project_root=tmp_path)
    script = pm._compose_remote_script(
        remote_file="/path/pkg.tar.gz",
        branch_name="test-branch",
        domain=None,
        ip=None,
    )
    # Always ensure --non-interactive is present and start-proxy suppresses stderr
    assert "--non-interactive" in script
    assert "start-proxy --non-interactive >/dev/null 2>&1" in script

