"""Phase 6 structural hardening tests.

- C1: the cross-class API boundary between SlimDeployManager and its helpers
      (RemoteDiskManager, DeployPreflight) uses public method names, not the
      leading-underscore "private" convention.
- C3: the single server prep script exists at ``ilha/scripts/server_prep.sh``.
- C4: the zero-downtime deploy script lives in a file (not an embedded Python
      f-string) and the B4 rollback guards survived the extraction.
"""

from pathlib import Path

from ilha.commands.push.remote_disk import RemoteDiskManager
from ilha.commands.push.deploy_preflight import DeployPreflight
from ilha.commands.push.slim_deploy_manager import SlimDeployManager


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "ilha" / "scripts"


# ── C1: cross-class API is public ───────────────────────────────────────

def test_remote_disk_manager_exposes_public_cross_class_api():
    """Every RemoteDiskManager method that SlimDeployManager calls must be
    public (no leading underscore). The underscore signalled "private", but
    SlimDeployManager was reaching across the class boundary anyway — so the
    convention was lying. Phase 6 made it honest."""
    public_api = {
        "check_remote_disk_capacity",
        "log_remote_disk_diagnostics",
        "attempt_remote_disk_recovery",
        "print_disk_recovery_guidance",
    }
    for name in public_api:
        assert hasattr(RemoteDiskManager, name), (
            f"RemoteDiskManager.{name} missing (Phase 6 rename regressed?)"
        )
        assert not name.startswith("_"), f"{name} must be public"


def test_deploy_preflight_exposes_public_cross_class_api():
    public_api = {
        "run_deploy_preflight",
        "resolve_worktree_path",
        "resolve_comparison_root",
        "gather_source_manifest",
        "build_drift_report",
        "detect_env_contract_issues",
    }
    for name in public_api:
        assert hasattr(DeployPreflight, name), (
            f"DeployPreflight.{name} missing (Phase 6 rename regressed?)"
        )
        assert not name.startswith("_"), f"{name} must be public"


# ── C3: single server prep script ───────────────────────────────────────

def test_server_prep_script_is_the_only_variant():
    prep = SCRIPTS_DIR / "server_prep.sh"
    assert prep.exists(), "ilha/scripts/server_prep.sh must exist"


# ── C4: ZD script is file-based, not an embedded f-string ───────────────

def test_zd_deploy_script_exists_as_file():
    zd = SCRIPTS_DIR / "zd_deploy.sh"
    assert zd.exists(), (
        "ilha/scripts/zd_deploy.sh must exist — the zero-downtime deploy "
        "logic was extracted out of slim_deploy_manager.py in Phase 6 so it "
        "can be shellcheck'd and diff-reviewed."
    )


def test_zd_deploy_script_uses_env_vars_not_python_interpolation():
    """The extracted script must consume shell env vars. If someone renames
    e.g. WEB_SERVICE, this test fails before a real deploy surfaces it."""
    body = (SCRIPTS_DIR / "zd_deploy.sh").read_text()
    required = [
        "APP_ROOT",
        "WEB_SERVICE",
        "WEB_PORT",
        "HEALTH_PATH",
        "DB_SERVICE",
        "DEPS_SERVICES",
        "WORKER_SERVICES",
        "BUILD_WEB",
    ]
    for name in required:
        assert f": \"${{{name}:" in body or f'"${name}"' in body or f"${name}" in body, (
            f"zd_deploy.sh must reference ${name}"
        )

    # Hard no: the script should not contain leftover Python f-string marker
    # patterns (e.g. `{web_service}`) that would indicate a bad merge.
    # (Note: `{{...}}` is legit — docker inspect --format uses Go templates.)
    for token in ("{web_service}", "{web_port}", "{health_path}", "{db_service}", "{deps_args}"):
        assert token not in body, f"stale Python f-string placeholder: {token}"


def test_zd_deploy_script_keeps_b4_rollback_guard():
    """B4: Phase 8 must still refuse to swap Caddy when NEW_HEALTHY != 1 and
    exit non-zero with the trap disarmed. Extraction to a file must not have
    lost this invariant."""
    body = (SCRIPTS_DIR / "zd_deploy.sh").read_text()
    assert 'NEW_HEALTHY" != "1"' in body
    guard_idx = body.find('NEW_HEALTHY" != "1"')
    phase9_idx = body.find("Phase 9:")
    assert guard_idx != -1 and phase9_idx != -1 and guard_idx < phase9_idx, (
        "The NEW_HEALTHY guard must appear before Phase 9 cleanup"
    )
    rollback_region = body[guard_idx:phase9_idx]
    assert "trap - EXIT" in rollback_region, "rollback must disarm the cleanup trap"
    assert "exit 1" in rollback_region, "rollback must exit non-zero"
    assert "leaving Caddy upstream on canary" in rollback_region


def test_build_zd_deploy_script_prepends_exports(tmp_path: Path):
    """Given a rendered script, the exports header must precede the body."""
    mgr = SlimDeployManager(project_root=tmp_path)
    script = mgr._build_zd_deploy_script(
        app_root="/app",
        web_service="api",
        web_port=3000,
        health_path="/healthz",
        db_service="postgres",
        deps_args="postgres cache",
        worker_args="",
        build_web=True,
    )
    assert script.startswith("#!/bin/bash\n")
    # Header order + values.
    header_end = script.index("\nset -euo pipefail")
    header = script[:header_end]
    assert "export APP_ROOT=/app" in header
    assert "export WEB_SERVICE=api" in header
    assert "export WEB_PORT=3000" in header
    assert "export HEALTH_PATH=/healthz" in header
    assert "export DB_SERVICE=postgres" in header
    assert "export DEPS_SERVICES='postgres cache'" in header
    assert "export WORKER_SERVICES=''" in header
    assert "export BUILD_WEB=1" in header
    # Body must start after exports (not earlier).
    assert "set -euo pipefail" in script[header_end:]


def test_build_zd_deploy_script_sets_build_web_false_when_not_building(tmp_path: Path):
    mgr = SlimDeployManager(project_root=tmp_path)
    script = mgr._build_zd_deploy_script(
        app_root="/app",
        web_service="web",
        web_port=8000,
        health_path="/health-check/",
        db_service="db",
        deps_args="db redis",
        worker_args="rq-worker-1",
        build_web=False,
    )
    assert "export BUILD_WEB=0" in script
