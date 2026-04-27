"""Phase 6 structural hardening tests.

- C1: the cross-class API boundary between SlimDeployManager and its helpers
      (RemoteDiskManager, DeployPreflight) uses public method names, not the
      leading-underscore "private" convention.
- C3: the single server prep script exists as a Python script at ``ilha/scripts/server_prep.py``.
- C4: the remote deploy script lives in a Python file at ``ilha/scripts/remote_deploy.py``
      with reusable utilities in ``ilha/scripts/deploy_utils.py``.
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

def test_server_prep_script_is_python():
    """Server prep is now a Python script for DRY, type-safe deployment."""
    prep = SCRIPTS_DIR / "server_prep.py"
    assert prep.exists(), "ilha/scripts/server_prep.py must exist"

    # Also check that deploy_utils exists (required dependency)
    utils = SCRIPTS_DIR / "deploy_utils.py"
    assert utils.exists(), "ilha/scripts/deploy_utils.py must exist (required by server_prep.py)"


# ── C4: remote deploy script is file-based and Python ───────────────────

def test_remote_deploy_script_exists_as_python():
    """Remote deploy is a Python script for DRY, type-safe deployment."""
    remote_deploy = SCRIPTS_DIR / "remote_deploy.py"
    assert remote_deploy.exists(), (
        "ilha/scripts/remote_deploy.py must exist — the remote deploy logic "
        "was migrated to Python in Phase 2 for DRY principles and type safety."
    )
    body = remote_deploy.read_text()
    assert "_persist_failure_diagnostics" in body
    assert "/tmp/ilha-diagnostics" in body


def test_remote_deploy_script_uses_env_vars():
    """The Python deployment script must read environment variables."""
    body = (SCRIPTS_DIR / "remote_deploy.py").read_text()
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
        assert name in body, f"remote_deploy.py must reference environment variable {name}"


def test_remote_deploy_imports_utilities():
    """remote_deploy.py must import the DRY utility classes."""
    body = (SCRIPTS_DIR / "remote_deploy.py").read_text()
    assert "from deploy_utils import" in body
    assert "DeployLogger" in body
    assert "DockerCommand" in body
