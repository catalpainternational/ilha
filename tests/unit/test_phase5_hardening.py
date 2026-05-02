"""Regression tests for Phase 5 deploy hardening.

Covers:
- B2: SSH execute_remote_script always cleans up /tmp file, even on failure.
- B6: DeployServiceConfig defaults match historical hardcoded values and flow
      into the remote start-app script and the smoke-probe command.
- B8: SSH control paths include the port, so simultaneous connections to the
      same host on different ports do not collide.
- G4: SSH host-key pinning refuses a stored known_hosts entry whose
      fingerprint does not match the caller's expected fingerprint.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from ilha.commands.push.deploy_service_config import (
    DeployServiceConfig,
    load_deploy_service_config,
)
from ilha.commands.push.slim_deploy_manager import SlimDeployManager
from ilha.utils.ssh_manager import SSHConnectionManager


# ── B2: /tmp cleanup survives script failure ─────────────────────────────

def test_execute_remote_script_always_cleans_up_tmp():
    """B2: the shell we hand to SSH must `rm` the /tmp script unconditionally
    (under the same rc), even when the uploaded script exits non-zero.

    The previous chain `cat > … && chmod … && /tmp/… && rm -f /tmp/…` skipped
    the `rm` whenever the script returned non-zero, accumulating stale files.
    """
    mgr = SSHConnectionManager()
    mgr.ensure_host_key = lambda *a, **kw: True  # type: ignore[method-assign]
    mgr.build_ssh_command = lambda *a, **kw: ["echo", a[2] if len(a) > 2 else ""]  # type: ignore[method-assign]

    captured = {}

    def fake_run(cmd, **kwargs):
        # Last argument is the remote command string the SSH wrapper passes.
        captured["remote_cmd"] = cmd[-1] if cmd else ""
        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    with patch("ilha.utils.ssh_manager.subprocess.run", side_effect=fake_run):
        mgr.execute_remote_script("root", "1.2.3.4", "echo hi", script_name="job.sh")

    remote = captured["remote_cmd"]
    assert "cat > /tmp/job.sh" in remote
    # Key assertions: not a short-circuiting && chain that skips rm.
    assert "&& rm -f /tmp/job.sh" not in remote, (
        "Old pattern: `&& rm` only runs when the script succeeds. "
        "Must capture rc and rm unconditionally instead."
    )
    assert "rc=$?" in remote, "Must capture the script's exit code"
    assert "rm -f /tmp/job.sh" in remote, "Must always attempt the cleanup"
    assert "exit $rc" in remote, "Must propagate the original exit code"


# ── B8: SSH control paths disambiguate by port ───────────────────────────

def test_ssh_control_path_includes_port():
    """B8: the control-master socket filename must encode the port so the
    manager does not confuse two simultaneous sessions to the same host on
    different SSH ports (e.g. :22 vs :2222 through a jumphost)."""
    mgr = SSHConnectionManager()
    path_22 = mgr._get_control_path("root", "1.2.3.4", 22)
    path_2222 = mgr._get_control_path("root", "1.2.3.4", 2222)

    assert path_22 != path_2222, (
        "Control paths must differ across ports on the same host"
    )
    assert path_22.endswith("-22"), f"Port 22 path should end with -22, got {path_22}"
    assert path_2222.endswith("-2222"), f"Port 2222 path should end with -2222, got {path_2222}"

    # Repeat calls must be stable (cached).
    assert mgr._get_control_path("root", "1.2.3.4", 22) == path_22


# ── G4: pinned fingerprint rejects a mismatched known_hosts entry ────────

def test_add_ssh_host_key_rejects_mismatched_stored_key(tmp_path: Path, monkeypatch):
    """G4: a stale TOFU entry in known_hosts must NOT be silently trusted when
    the caller pins a specific fingerprint. Previously the early-out at the
    top of ``add_ssh_host_key`` returned True on any ssh-keygen -F hit,
    bypassing verification."""
    from ilha.utils import ssh_utils

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(ssh_utils.Path, "home", lambda: home)

    ssh_dir = home / ".ssh"
    ssh_dir.mkdir()
    known_hosts = ssh_dir / "known_hosts"
    # Any non-empty content; we stub the ssh-keygen -F subprocess anyway.
    known_hosts.write_text("placeholder\n")

    # Fake ssh-keygen -F hit with a key whose computed fingerprint we control.
    stored_fps = ["SHA256:aaaabbbbccccdddd"]
    monkeypatch.setattr(
        ssh_utils, "_compute_key_fingerprints", lambda _out: stored_fps
    )

    class KnownHostsHit:
        returncode = 0
        stdout = "host,1.2.3.4 ssh-ed25519 AAAAfakekey"
        stderr = ""

    def fake_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "ssh-keygen" and "-F" in cmd:
            return KnownHostsHit()
        raise AssertionError(
            f"Unexpected subprocess.run call during lookup phase: {cmd!r}"
        )

    monkeypatch.setattr(ssh_utils.subprocess, "run", fake_run)

    # Expected pin does NOT match the stored fingerprint — must be rejected.
    ok = ssh_utils.add_ssh_host_key(
        "1.2.3.4", expected_fingerprint="SHA256:zzzzzzzzzzzzzzzz"
    )
    assert ok is False, (
        "A stored host-key entry whose fingerprint does not match the pinned "
        "fingerprint must be rejected; silent acceptance would defeat the pin."
    )

    # Control: matching pin accepts the stored entry without keyscan.
    ok_match = ssh_utils.add_ssh_host_key("1.2.3.4", expected_fingerprint=stored_fps[0])
    assert ok_match is True


# ── B6: DeployServiceConfig ──────────────────────────────────────────────

def test_deploy_service_config_defaults_match_historical():
    cfg = DeployServiceConfig()
    assert cfg.web_service == "web"
    assert cfg.web_port == 8000
    assert cfg.health_path == "/health-check/"
    assert cfg.dependency_services == ["db", "redis"]
    assert cfg.db_service == "db"
    assert cfg.worker_services == ["rq-worker-1"]


def test_deploy_service_config_from_mapping_overrides():
    cfg = DeployServiceConfig.from_mapping({
        "web_service": "api",
        "web_port": 3000,
        "health_path": "healthz",  # missing leading slash on purpose
        "dependency_services": "postgres, cache",  # comma-string convenience
        "worker_services": [],
    })
    assert cfg.web_service == "api"
    assert cfg.web_port == 3000
    assert cfg.health_path == "/healthz"
    assert cfg.dependency_services == ["postgres", "cache"]
    assert cfg.worker_services == []


def test_deploy_service_config_invalid_port_falls_back(caplog):
    cfg = DeployServiceConfig.from_mapping({"web_port": "not-a-number"})
    assert cfg.web_port == 8000  # default preserved


def test_load_deploy_service_config_missing_file_returns_defaults(tmp_path: Path):
    """No config and no compose file → empty deps/workers/db (nothing to infer)."""
    cfg = load_deploy_service_config(tmp_path)
    assert cfg.web_service == "web"
    assert cfg.web_port == 8000
    assert cfg.health_path == "/health-check/"
    assert cfg.dependency_services == []
    assert cfg.db_service == ""
    assert cfg.worker_services == []


def test_load_deploy_service_config_reads_yaml(tmp_path: Path):
    (tmp_path / ".ilha").mkdir()
    (tmp_path / ".ilha" / "config.yml").write_text(
        "deploy:\n"
        "  web_service: api\n"
        "  web_port: 3000\n"
        "  health_path: /healthz\n"
        "  worker_services: []\n"
    )
    cfg = load_deploy_service_config(tmp_path)
    assert cfg.web_service == "api"
    assert cfg.web_port == 3000
    assert cfg.health_path == "/healthz"
    assert cfg.worker_services == []


def test_load_deploy_service_config_smoke_path_default_and_override(tmp_path: Path):
    """``smoke_path`` defaults to ``None`` (= use detection); explicit value wins."""
    cfg = load_deploy_service_config(tmp_path)
    assert cfg.smoke_path is None  # no override → preflight detection runs

    (tmp_path / ".ilha").mkdir()
    (tmp_path / ".ilha" / "config.yml").write_text(
        "deploy:\n"
        "  smoke_path: /api/health/\n"
    )
    cfg = load_deploy_service_config(tmp_path)
    assert cfg.smoke_path == "/api/health/"


def test_smoke_path_env_var_overrides_config_and_detection(tmp_path: Path, monkeypatch):
    """``ILHA_SMOKE_PATH`` wins over ``deploy.smoke_path`` and preflight detection."""
    (tmp_path / ".ilha").mkdir()
    (tmp_path / ".ilha" / "config.yml").write_text(
        "deploy:\n"
        "  smoke_path: /from/config/\n"
    )
    monkeypatch.setenv("ILHA_SMOKE_PATH", "/from/env/")
    mgr = SlimDeployManager(project_root=tmp_path)
    # Stub preflight to isolate the override layer.
    monkeypatch.setattr(
        mgr.preflight,
        "run_deploy_preflight",
        lambda *a, **kw: {
            "manifest": {}, "drift_report": {}, "smoke_path": "/detected/",
        },
    )
    result = mgr._run_deploy_preflight("main")
    assert result["smoke_path"] == "/from/env/"

    # Without env var: config.yml wins over detection.
    monkeypatch.delenv("ILHA_SMOKE_PATH", raising=False)
    result = mgr._run_deploy_preflight("main")
    assert result["smoke_path"] == "/from/config/"


# ── B6: config flows into the remote start-app script ───────────────────

def _render_start_app_script(mgr: SlimDeployManager) -> str:
    """Return the remote deploy wrapper script (exports + ``python3 remote_deploy.py``)."""
    return mgr._compose_remote_deploy_wrapper_script(
        app_root=mgr.REMOTE_APP_ROOT,
        remote_tmp_dir="/tmp/ilha-testsession",
        remote_deploy_file="/tmp/ilha-testsession/remote_deploy.py",
        build=False,
    )


def test_start_app_script_honors_custom_service_config(tmp_path: Path):
    """A project that configures `deploy:` in its config.yml must see those
    values exported to the start-app script — not the hardcoded defaults.

    Post Phase 6: service config is passed as shell env vars to the wrapper that
    runs ``remote_deploy.py`` on the server.
    """
    (tmp_path / ".ilha").mkdir()
    (tmp_path / ".ilha" / "config.yml").write_text(
        "deploy:\n"
        "  web_service: api\n"
        "  web_port: 3000\n"
        "  health_path: /healthz\n"
        "  dependency_services: [postgres, cache]\n"
        "  db_service: postgres\n"
        "  worker_services: [celery-worker]\n"
    )
    mgr = SlimDeployManager(project_root=tmp_path)
    script = _render_start_app_script(mgr)

    # Config-driven exports appear in the header.
    assert "export WEB_SERVICE=api" in script
    assert "export WEB_PORT=3000" in script
    assert "export HEALTH_PATH=/healthz" in script
    assert "export DB_SERVICE=postgres" in script
    assert "export DEPS_SERVICES='postgres cache'" in script
    assert "export WORKER_SERVICES=celery-worker" in script

    assert "python3" in script and "remote_deploy.py" in script

    # Historical hardcoded values must NOT appear in functional positions.
    assert ":8000/" not in script
    assert "rq-worker-1" not in script


def test_start_app_script_infers_services_from_compose_without_config(tmp_path: Path):
    """No .ilha/config.yml, but a compose file declaring db/redis/rq-worker-1
    should yield those services in the start-app script via pure inference —
    no historical hardcoded names required."""
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  web:\n"
        "    build: .\n"
        "    depends_on: [db, redis]\n"
        "  db:\n"
        "    image: postgres:16-alpine\n"
        "  redis:\n"
        "    image: redis:7-alpine\n"
        "  rq-worker-1:\n"
        "    build: .\n"
        "    command: rq worker default\n"
    )
    mgr = SlimDeployManager(project_root=tmp_path)
    script = _render_start_app_script(mgr)
    assert "export WEB_SERVICE=web" in script
    assert "export WEB_PORT=8000" in script
    assert "export HEALTH_PATH=/health-check/" in script
    assert "export DB_SERVICE=db" in script
    assert "export DEPS_SERVICES='db redis'" in script
    assert "export WORKER_SERVICES=rq-worker-1" in script


def test_start_app_script_omits_services_not_in_compose(tmp_path: Path):
    """No config, and a compose without redis/workers → script must not export
    them (regression for projects that previously had db/redis/rq-worker-1
    forced on them by hardcoded defaults)."""
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  web:\n"
        "    build: .\n"
        "    depends_on: [db]\n"
        "  db:\n"
        "    image: postgres:16-alpine\n"
    )
    mgr = SlimDeployManager(project_root=tmp_path)
    script = _render_start_app_script(mgr)
    assert "export DB_SERVICE=db" in script
    assert "export DEPS_SERVICES=db" in script  # shlex.quote omits quotes for single bare token
    # No redis / no workers in compose → must not appear.
    assert "redis" not in script
    assert "rq-worker" not in script
    assert "export WORKER_SERVICES=''" in script


def test_start_app_script_no_worker_services_skips_phase10(tmp_path: Path):
    (tmp_path / ".ilha").mkdir()
    (tmp_path / ".ilha" / "config.yml").write_text(
        "deploy:\n"
        "  worker_services: []\n"
    )
    mgr = SlimDeployManager(project_root=tmp_path)
    script = _render_start_app_script(mgr)
    # Empty worker list is exported; Phase 10 in remote_deploy.py logs "No workers configured".
    assert "export WORKER_SERVICES=" in script
    remote_body = (Path(__file__).resolve().parents[2] / "ilha" / "scripts" / "remote_deploy.py").read_text()
    assert "No workers configured" in remote_body


# ── Phase 3: Django+SPA Caddy path routing ──────────────────────────────

def test_django_spa_inference_populates_frontend_and_api_paths(tmp_path: Path):
    """A django_spa project gets ``frontend_service`` + default ``api_paths``."""
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  web:\n"
        "    build: .\n"
        "    command: daphne config.asgi:application\n"
        "  frontend:\n"
        "    image: node:20-alpine\n"
        "    command: npm run dev\n"
    )
    cfg = load_deploy_service_config(tmp_path)
    assert cfg.web_service == "web"
    assert cfg.frontend_service == "frontend"
    assert cfg.api_paths is not None
    assert "/api/*" in cfg.api_paths
    assert "/admin" in cfg.api_paths and "/admin/*" in cfg.api_paths


def test_probe_content_type_matches(tmp_path: Path, monkeypatch):
    """Headers contain the expected Content-Type substring → success."""
    mgr = SlimDeployManager(project_root=tmp_path)
    headers = (
        "HTTP/2 200\r\n"
        "content-type: text/css; charset=utf-8\r\n"
        "content-length: 4242\r\n\r\n"
    )
    monkeypatch.setattr(
        mgr, "_run_local_text", lambda *a, **kw: (0, headers, "")
    )
    ok, err = mgr._probe_external_path_content_type(
        "ex.com", "/static/admin/css/base.css", "text/css"
    )
    assert ok is True
    assert err is None


def test_probe_content_type_rejects_html_when_css_expected(tmp_path: Path, monkeypatch):
    """SPA-fallback trap: path returns 200 but as text/html (the index.html).
    Probe must catch this even though curl ``-fsS`` says success."""
    mgr = SlimDeployManager(project_root=tmp_path)
    headers = "HTTP/2 200\r\ncontent-type: text/html; charset=utf-8\r\n\r\n"
    monkeypatch.setattr(
        mgr, "_run_local_text", lambda *a, **kw: (0, headers, "")
    )
    ok, err = mgr._probe_external_path_content_type(
        "ex.com", "/static/admin/css/base.css", "text/css"
    )
    assert ok is False
    assert "text/css" in err
    assert "text/html" in err  # surfaces what was actually returned


def test_probe_content_type_curl_failure(tmp_path: Path, monkeypatch):
    mgr = SlimDeployManager(project_root=tmp_path)
    monkeypatch.setattr(
        mgr, "_run_local_text",
        lambda *a, **kw: (22, "", "curl: (22) HTTP 404"),
    )
    ok, err = mgr._probe_external_path_content_type(
        "ex.com", "/static/admin/css/base.css", "text/css"
    )
    assert ok is False
    assert "404" in err


def test_django_spa_with_asgi_backend_includes_ws_in_api_paths(tmp_path: Path):
    """Backend on Daphne/Uvicorn → ``/ws/*`` joins api_paths so websocket
    upgrades route to the backend instead of falling into the SPA catch-all."""
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  web:\n"
        "    build: .\n"
        "    command: daphne -b 0.0.0.0 -p 8000 config.asgi:application\n"
        "  frontend:\n"
        "    image: node:20-alpine\n"
        "    command: npm run dev\n"
    )
    cfg = load_deploy_service_config(tmp_path)
    assert "/ws/*" in cfg.api_paths


def test_django_spa_without_asgi_omits_ws(tmp_path: Path):
    """Gunicorn (WSGI) backend → no ``/ws/*`` in defaults (conservative;
    user can still add it via deploy.api_paths if their frontend doesn't
    need /ws/*)."""
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  web:\n"
        "    build: .\n"
        "    command: gunicorn config.wsgi:application\n"
        "  frontend:\n"
        "    image: node:20-alpine\n"
        "    command: npm run dev\n"
    )
    cfg = load_deploy_service_config(tmp_path)
    assert "/ws/*" not in (cfg.api_paths or [])


def test_solo_django_inference_leaves_api_paths_none(tmp_path: Path):
    """No frontend → no SPA routing → api_paths stays None."""
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  web:\n"
        "    build: .\n"
        "    command: gunicorn config.wsgi\n"
        "  db:\n"
        "    image: postgres:16-alpine\n"
    )
    cfg = load_deploy_service_config(tmp_path)
    assert cfg.frontend_service is None
    assert cfg.api_paths is None


def test_start_app_script_exports_frontend_service_for_django_spa(tmp_path: Path):
    """A django_spa project must export FRONTEND_SERVICE so remote_deploy.py
    brings up the SPA container alongside web + workers."""
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  web:\n"
        "    build: .\n"
        "    command: daphne config.asgi:application\n"
        "  frontend:\n"
        "    image: node:20-alpine\n"
        "    command: npm run dev\n"
    )
    mgr = SlimDeployManager(project_root=tmp_path)
    script = mgr._compose_remote_deploy_wrapper_script(
        app_root=mgr.REMOTE_APP_ROOT,
        remote_tmp_dir="/tmp/ilha-test",
        remote_deploy_file="/tmp/ilha-test/remote_deploy.py",
        build=False,
    )
    assert "export FRONTEND_SERVICE=frontend" in script


def test_start_app_script_frontend_service_empty_for_solo_django(tmp_path: Path):
    """Solo-django project: FRONTEND_SERVICE is empty so remote_deploy skips
    the frontend bring-up step."""
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  web:\n"
        "    build: .\n"
        "    command: gunicorn config.wsgi\n"
        "  db:\n"
        "    image: postgres:16-alpine\n"
    )
    mgr = SlimDeployManager(project_root=tmp_path)
    script = mgr._compose_remote_deploy_wrapper_script(
        app_root=mgr.REMOTE_APP_ROOT,
        remote_tmp_dir="/tmp/ilha-test",
        remote_deploy_file="/tmp/ilha-test/remote_deploy.py",
        build=False,
    )
    assert "export FRONTEND_SERVICE=''" in script


def test_probe_remote_existing_volumes_filters_to_requested(tmp_path: Path, monkeypatch):
    """The probe filters ``docker volume inspect`` output back to the names
    the caller asked about — protects against any stray volume on the
    droplet bleeding into the warning."""
    mgr = SlimDeployManager(project_root=tmp_path)
    monkeypatch.setattr(
        mgr, "_run_remote_text",
        lambda *a, **kw: (0, "time_pgdata\nunrelated_volume\n", ""),
    )
    existing = mgr._probe_remote_existing_volumes(
        "root", "1.2.3.4", ["time_pgdata", "time_frontend_node_modules"]
    )
    assert existing == ["time_pgdata"]


def test_warn_about_existing_remote_volumes_emits_when_any_exist(tmp_path: Path, monkeypatch):
    """Operator-visible warning fires when restore is about to overwrite
    a server-side volume that's already present."""
    mgr = SlimDeployManager(project_root=tmp_path)
    monkeypatch.setattr(
        mgr, "_run_remote_text",
        lambda *a, **kw: (0, "time_pgdata\n", ""),
    )

    captured: List[str] = []
    monkeypatch.setattr(
        "ilha.commands.push.slim_deploy_manager.log_warning",
        captured.append,
    )

    mgr._warn_about_existing_remote_volumes(
        "root", "1.2.3.4", ["time_pgdata", "time_frontend_node_modules"]
    )
    joined = " ".join(captured)
    assert "OVERWRITE" in joined
    assert "time_pgdata" in joined
    assert "--no-restore-volumes" in joined


def test_warn_about_existing_remote_volumes_silent_when_none_exist(tmp_path: Path, monkeypatch):
    """First-deploy migration (server has no volumes yet) → no warning,
    no log noise."""
    mgr = SlimDeployManager(project_root=tmp_path)
    monkeypatch.setattr(
        mgr, "_run_remote_text",
        lambda *a, **kw: (0, "", ""),  # no volumes exist on server
    )

    captured: List[str] = []
    monkeypatch.setattr(
        "ilha.commands.push.slim_deploy_manager.log_warning",
        captured.append,
    )

    mgr._warn_about_existing_remote_volumes(
        "root", "1.2.3.4", ["time_pgdata"]
    )
    assert captured == []


def test_strip_privileges_in_compose_also_strips_host_port_publishes(tmp_path: Path):
    """Slim deploy should never expose db/redis/etc. on host ports — Caddy
    reaches them via the docker network. Compose v2's merge ignores
    ``ports: []`` from an overlay, so the only reliable wipe is to remove
    the key from the BASE compose copy at deploy assembly time."""
    import yaml
    base_path = tmp_path / "docker-compose.yml"
    base_path.write_text(
        "services:\n"
        "  web:\n"
        "    build: .\n"
        "    command: gunicorn config.wsgi\n"
        "    ports:\n"
        "      - 8000:8000\n"
        "  db:\n"
        "    image: postgres:16-alpine\n"
        "    ports:\n"
        "      - 5433:5432\n"
        "  redis:\n"
        "    image: redis:7-alpine\n"
        "    ports:\n"
        "      - 6379:6379\n"
    )

    mgr = SlimDeployManager(project_root=tmp_path)
    mgr._strip_privileges_in_compose(base_path)

    data = yaml.safe_load(base_path.read_text())
    for svc_name, svc_config in (data.get("services") or {}).items():
        assert "ports" not in svc_config, (
            f"Service '{svc_name}' must not declare host ports in the deploy "
            f"compose copy; got: {svc_config.get('ports')!r}"
        )


def test_caddy_path_label_emitted_for_django_spa_overlay(tmp_path: Path, monkeypatch):
    """``_apply_compose_overrides`` emits ``caddy.proxy.path`` for the backend
    in a django_spa project, leaving the frontend as the catch-all."""
    import yaml
    (tmp_path / "docker-compose.yml").write_text(
        "services:\n"
        "  web:\n"
        "    build: .\n"
        "    command: daphne config.asgi:application\n"
        "  frontend:\n"
        "    image: node:20-alpine\n"
        "    command: npm run dev\n"
    )
    overlay_path = tmp_path / ".ilha" / "docker-compose.overlay.yml"
    overlay_path.parent.mkdir(parents=True)
    overlay_path.write_text(
        "services:\n"
        "  web:\n"
        "    labels:\n"
        "      - caddy.proxy=time.localhost\n"
        "      - caddy.proxy.reverse_proxy=time-web:8000\n"
        "  frontend:\n"
        "    labels:\n"
        "      - caddy.proxy=time.localhost\n"
        "      - caddy.proxy.reverse_proxy=time-frontend:5173\n"
    )

    mgr = SlimDeployManager(project_root=tmp_path)
    mgr._apply_compose_overrides(overlay_path, domain="example.com")

    data = yaml.safe_load(overlay_path.read_text())
    web_labels = data["services"]["web"]["labels"]
    fe_labels = data["services"]["frontend"]["labels"]

    web_path_labels = [l for l in web_labels if l.startswith("caddy.proxy.path=")]
    assert len(web_path_labels) == 1
    assert "/api/*" in web_path_labels[0]
    assert "/admin/*" in web_path_labels[0]

    # Frontend gets no path label = catch-all.
    assert not any(l.startswith("caddy.proxy.path=") for l in fe_labels)
