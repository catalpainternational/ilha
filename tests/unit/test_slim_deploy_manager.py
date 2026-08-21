import json
from pathlib import Path

from ilha.commands.push.slim_deploy_manager import SlimDeployManager


def test_enospc_marker_detection():
    assert SlimDeployManager._is_enospc_line("write ... no space left on device")
    assert SlimDeployManager._is_enospc_line("ERROR: failed to extract layer sha256:abc")
    assert not SlimDeployManager._is_enospc_line("container started successfully")


def test_pattern_matches_artifact_paths():
    assert SlimDeployManager._pattern_matches("packages/archive.ilha-package.tar.gz", "archive.ilha-package.tar.gz", "packages/")
    assert SlimDeployManager._pattern_matches("packages/archive.ilha-package.tar.gz", "archive.ilha-package.tar.gz", "*.ilha-package.tar.gz")
    assert SlimDeployManager._pattern_matches(".pytest_cache/v/cache", "cache", ".pytest_cache")
    assert not SlimDeployManager._pattern_matches("src/app.py", "app.py", "packages/")


def test_load_deployignore_patterns(tmp_path: Path):
    root = tmp_path / "worktrees" / "demo"
    ignore_file = root / ".ilha" / "deployignore"
    ignore_file.parent.mkdir(parents=True)
    ignore_file.write_text("# comment\nbuild/\n*.tmp\n")
    mgr = SlimDeployManager(project_root=tmp_path)
    patterns = mgr._load_deployignore_patterns(root, None)
    assert "build/" in patterns
    assert "*.tmp" in patterns


def test_disk_preflight_detects_insufficient_space(tmp_path: Path, monkeypatch):
    mgr = SlimDeployManager(project_root=tmp_path)

    def fake_remote(*args, **kwargs):
        return 0, "/app:100\n/var/lib/docker:100\n", ""

    monkeypatch.setattr(mgr.disk, "_run_remote_text", fake_remote)
    ok, report = mgr.disk.check_remote_disk_capacity(
        username="root",
        server="1.2.3.4",
        deploy_size_bytes=1024 * 1024 * 200,
    )
    assert ok is False
    assert report["ok"] is False
    assert report["available_bytes"]["/app"] == 100


def test_disk_preflight_missing_app_path_uses_parent_filesystem(tmp_path: Path, monkeypatch):
    mgr = SlimDeployManager(project_root=tmp_path)

    def fake_remote(*args, **kwargs):
        return 0, "/app:2147483648:/\n/var/lib/docker:2147483648:/var/lib/docker\n", ""

    monkeypatch.setattr(mgr.disk, "_run_remote_text", fake_remote)
    ok, report = mgr.disk.check_remote_disk_capacity(
        username="root",
        server="1.2.3.4",
        deploy_size_bytes=1024 * 1024 * 200,
    )
    assert ok is True
    assert report["ok"] is True
    assert report["available_bytes"]["/app"] == 2147483648
    assert report["probe_paths"]["/app"] == "/"


def test_disk_preflight_report_contains_probe_paths(tmp_path: Path, monkeypatch):
    mgr = SlimDeployManager(project_root=tmp_path)

    def fake_remote(*args, **kwargs):
        return 0, "/app:2147483648:/\n/var/lib/docker:2147483648:/var/lib/docker\n", ""

    monkeypatch.setattr(mgr.disk, "_run_remote_text", fake_remote)
    ok, report = mgr.disk.check_remote_disk_capacity(
        username="root",
        server="1.2.3.4",
        deploy_size_bytes=10,
    )
    assert ok is True
    assert report["probe_paths"]["/app"] == "/"
    assert report["probe_paths"]["/var/lib/docker"] == "/var/lib/docker"


def test_disk_recovery_modes(tmp_path: Path, monkeypatch):
    mgr = SlimDeployManager(project_root=tmp_path)
    captured = {}

    def fake_remote(_u, _s, command, timeout=30):
        captured["command"] = command
        return 0, "", ""

    monkeypatch.setattr(mgr.disk, "_run_remote_text", fake_remote)
    assert mgr.disk.attempt_remote_disk_recovery("root", "1.2.3.4", "safe", force=True)
    assert "docker system prune -af --volumes" not in captured["command"]
    assert mgr.disk.attempt_remote_disk_recovery("root", "1.2.3.4", "aggressive", force=True)
    assert "docker system prune -af --volumes" in captured["command"]


def test_gather_source_manifest_collects_checksums(tmp_path: Path, monkeypatch):
    root = tmp_path / "repo"
    worktree = root / "worktrees" / "demo"
    (worktree / ".ilha").mkdir(parents=True)
    (worktree / "Dockerfile").write_text("FROM python:3.12-slim\n")
    (worktree / ".ilha" / "docker-compose.overlay.yml").write_text("services:\n  web:\n    image: app\n")
    (worktree / "pyproject.toml").write_text("[project]\nname='demo'\n")
    (worktree / "uv.lock").write_text("version = 1\n")

    mgr = SlimDeployManager(project_root=root)
    monkeypatch.setattr(mgr.preflight, "_get_git_branch", lambda _path: "demo")
    monkeypatch.setattr(mgr.preflight, "_is_git_dirty", lambda _path: True)

    manifest = mgr.preflight.gather_source_manifest("demo", worktree, root)
    assert manifest["branch_name"] == "demo"
    assert manifest["source_path"] == str(worktree)
    assert manifest["comparison_root"] == str(root)
    assert manifest["git_branch"] == "demo"
    assert "git_sha" in manifest
    assert manifest["git_dirty"] is True
    assert manifest["checksums"]["Dockerfile"] is not None
    assert manifest["checksums"][".ilha/docker-compose.overlay.yml"] is not None
    assert manifest["checksums"]["pyproject.toml"] is not None
    assert manifest["checksums"]["uv.lock"] is not None


def test_build_drift_report_warns_for_dockerfile_and_fails_for_compose(tmp_path: Path):
    root = tmp_path / "repo"
    worktree = root / "worktrees" / "demo"
    (root / ".ilha").mkdir(parents=True)
    (worktree / ".ilha").mkdir(parents=True)
    (root / "Dockerfile").write_text("FROM python:3.12\n")
    (worktree / "Dockerfile").write_text("FROM python:3.13\n")
    (root / ".ilha" / "docker-compose.overlay.yml").write_text("services:\n  web:\n    image: root\n")
    (worktree / ".ilha" / "docker-compose.overlay.yml").write_text("services:\n  web:\n    image: worktree\n")

    mgr = SlimDeployManager(project_root=root)
    report = mgr.preflight.build_drift_report(worktree, root)

    assert report["status"] == "error"
    severities = {entry["file"]: entry["severity"] for entry in report["differences"]}
    assert severities["Dockerfile"] == "warning"
    assert severities[".ilha/docker-compose.overlay.yml"] == "error"


def test_build_drift_report_skips_cross_tree_drift_when_heads_differ(tmp_path: Path, monkeypatch):
    root = tmp_path / "repo"
    worktree = root / "worktrees" / "demo"
    (root / ".ilha").mkdir(parents=True)
    (worktree / ".ilha").mkdir(parents=True)
    (root / ".ilha" / "docker-compose.overlay.yml").write_text("services:\n  web:\n    image: root\n")
    (worktree / ".ilha" / "docker-compose.overlay.yml").write_text("services:\n  web:\n    image: worktree\n")

    mgr = SlimDeployManager(project_root=root)
    monkeypatch.setattr(mgr.preflight, "_git_rev_parse_head", lambda path: "root-head" if path == root else "worktree-head")

    report = mgr.preflight.build_drift_report(worktree, root)

    assert report["status"] == "ok"
    assert report["differences"] == []
    assert report["skipped"] is True
    assert report["skip_reason"] == "different_git_heads"


def test_build_drift_report_strict_mode_keeps_drift_enforcement(tmp_path: Path, monkeypatch):
    root = tmp_path / "repo"
    worktree = root / "worktrees" / "demo"
    (root / ".ilha").mkdir(parents=True)
    (worktree / ".ilha").mkdir(parents=True)
    (root / ".ilha" / "docker-compose.overlay.yml").write_text("services:\n  web:\n    image: root\n")
    (worktree / ".ilha" / "docker-compose.overlay.yml").write_text("services:\n  web:\n    image: worktree\n")

    mgr = SlimDeployManager(project_root=root)
    monkeypatch.setattr(mgr.preflight, "_git_rev_parse_head", lambda _path: "same-head")

    report = mgr.preflight.build_drift_report(worktree, root, strict_deploy_drift=True)

    assert report["status"] == "error"
    assert report["skipped"] is False
    assert any(entry["file"] == ".ilha/docker-compose.overlay.yml" for entry in report["differences"])


def test_run_deploy_preflight_fails_on_critical_drift(tmp_path: Path, monkeypatch):
    root = tmp_path / "repo"
    worktree = root / "worktrees" / "demo"
    (root / ".ilha").mkdir(parents=True)
    (worktree / ".ilha").mkdir(parents=True)
    (root / ".ilha" / "docker-compose.overlay.yml").write_text("services:\n  web:\n    image: root\n")
    (worktree / ".ilha" / "docker-compose.overlay.yml").write_text("services:\n  web:\n    image: worktree\n")

    mgr = SlimDeployManager(project_root=root)
    monkeypatch.setattr(mgr.preflight, "_get_git_branch", lambda _path: "demo")
    monkeypatch.setattr(mgr.preflight, "_is_git_dirty", lambda _path: False)

    result = mgr._run_deploy_preflight("demo")
    assert result is None
    assert mgr.get_last_deploy_report()["failure_category"] == "validation_error"
    assert mgr.get_last_deploy_report()["drift_report"]["status"] == "error"


def test_detect_smoke_path_prefers_compose_label_then_repo_convention(tmp_path: Path):
    root = tmp_path / "repo"
    worktree = root / "worktrees" / "demo"
    (worktree / ".ilha").mkdir(parents=True)
    compose = worktree / ".ilha" / "docker-compose.overlay.yml"
    compose.write_text(
        "services:\n"
        "  web:\n"
        "    labels:\n"
        "      caddy.proxy.health_check: /healthz/\n"
    )

    mgr = SlimDeployManager(project_root=root)
    assert mgr.preflight._detect_smoke_path(compose, root) == "/healthz/"

    compose.write_text("services:\n  web:\n    image: app\n")
    (root / "documentation").mkdir()
    (root / "documentation" / "PROJECT_SETUP.md").write_text("Use /api/health/ for checks.\n")
    assert mgr.preflight._detect_smoke_path(compose, root) == "/api/health/"


def test_run_smoke_checks_skips_external_when_dns_not_ready(tmp_path: Path, monkeypatch):
    """Deploy should not fail when the local resolver lags behind DNS propagation."""
    mgr = SlimDeployManager(project_root=tmp_path)
    local_calls: list[list[str]] = []

    def capture_local(cmd, timeout=10):
        local_calls.append(list(cmd) if isinstance(cmd, list) else [])
        return 0, "", ""

    monkeypatch.setattr(mgr, "_remote_service_exists", lambda *a, **k: False)
    monkeypatch.setattr(mgr, "_run_remote_text", lambda *a, **k: (0, "", ""))
    monkeypatch.setattr(mgr, "_run_local_text", capture_local)
    monkeypatch.setattr("ilha.commands.push.slim_deploy_manager.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(mgr, "_wait_for_dns_resolution", lambda *a, **k: False)

    ok, report = mgr._run_smoke_checks(
        username="root",
        server="1.2.3.4",
        smoke_path="/",
        domain="slow.example.com",
        ip="1.2.3.4",
    )

    assert ok is True
    assert report.get("external_checks_skipped") == "dns_not_ready_locally"
    assert {check["name"] for check in report["checks"]} == {"local_app", "compose_health"}
    # No local curl to the public domain (would fail the same as deploy host).
    for cmd in local_calls:
        joined = " ".join(cmd)
        assert "slow.example.com" not in joined


def test_run_smoke_checks_retries_until_success(tmp_path: Path, monkeypatch):
    mgr = SlimDeployManager(project_root=tmp_path)
    calls = {"remote": 0, "local": 0}

    def fake_remote(*args, **kwargs):
        calls["remote"] += 1
        if calls["remote"] < 3:
            return 1, "", "connection refused"
        return 0, "", ""

    def fake_local(cmd, timeout=10):
        calls["local"] += 1
        argv = cmd if isinstance(cmd, list) else []
        joined = " ".join(argv)
        if "http://demo.example.com/accounts/login/" in joined:
            return 0, (
                "HTTP/1.1 308 Permanent Redirect\n"
                "Location: https://demo.example.com/accounts/login/\n\n"
            ), ""
        if "-D" in joined and "-o -" in joined and "https://" in joined:
            body = "x" * 300
            return 0, f"HTTP/2 200\nContent-Type: text/html; charset=utf-8\n\n{body}", ""
        return 0, "", ""

    monkeypatch.setattr(mgr, "_remote_service_exists", lambda *a, **k: False)
    monkeypatch.setattr(mgr, "_run_remote_text", fake_remote)
    monkeypatch.setattr(mgr, "_run_local_text", fake_local)
    monkeypatch.setattr("ilha.commands.push.slim_deploy_manager.time.sleep", lambda _seconds: None)
    monkeypatch.setattr(mgr, "_wait_for_dns_resolution", lambda *a, **k: True)

    ok, report = mgr._run_smoke_checks(
        username="root",
        server="1.2.3.4",
        smoke_path="/api/health/",
        domain="demo.example.com",
        ip=None,
    )

    assert ok is True
    assert report["success"] is True
    assert calls["remote"] == 4  # 3 HTTP probes + compose health
    assert calls["local"] == 3  # HTTP redirect + HEAD + HTML GET for domain smoke
    assert {check["name"] for check in report["checks"]} == {
        "local_app",
        "external_http_to_https_redirect",
        "external_domain_head",
        "external_domain_html",
        "compose_health",
    }


def test_run_smoke_checks_reports_failure(tmp_path: Path, monkeypatch):
    mgr = SlimDeployManager(project_root=tmp_path)
    monkeypatch.setattr(mgr, "_remote_service_exists", lambda *a, **k: False)
    monkeypatch.setattr(mgr, "_run_remote_text", lambda *args, **kwargs: (1, "", "boom"))
    monkeypatch.setattr("ilha.commands.push.slim_deploy_manager.time.sleep", lambda _seconds: None)

    ok, report = mgr._run_smoke_checks(
        username="root",
        server="1.2.3.4",
        smoke_path="/",
        domain=None,
        ip=None,
    )

    assert ok is False
    assert report["success"] is False
    assert report["checks"][0]["name"] == "local_app"
    assert "web_logs_tail" not in report


def test_http_redirect_probe_requires_https_location(tmp_path: Path, monkeypatch):
    mgr = SlimDeployManager(project_root=tmp_path)

    monkeypatch.setattr(
        mgr,
        "_run_local_text",
        lambda *a, **k: (
            0,
            "HTTP/1.1 200 OK\nContent-Type: text/html\n\n",
            "",
        ),
    )
    ok, err = mgr._probe_http_redirects_to_https("demo.example.com")

    assert ok is False
    assert "expected HTTP redirect" in (err or "")


def test_run_smoke_checks_failure_attaches_web_logs_when_probing_container(tmp_path: Path, monkeypatch):
    mgr = SlimDeployManager(project_root=tmp_path)
    monkeypatch.setattr(mgr, "_remote_service_exists", lambda *a, **k: True)
    monkeypatch.setattr("ilha.commands.push.slim_deploy_manager.time.sleep", lambda _seconds: None)

    def fake_remote(_u, _s, command, timeout=30):
        if "logs --tail" in command:
            return 0, "migration failed on startup\n", ""
        return 1, "", "curl failed"

    monkeypatch.setattr(mgr, "_run_remote_text", fake_remote)

    ok, report = mgr._run_smoke_checks(
        username="root",
        server="1.2.3.4",
        smoke_path="/",
        domain=None,
        ip=None,
        timeout_seconds=5,
    )

    assert ok is False
    assert "web_logs_tail" in report
    assert "migration failed" in report["web_logs_tail"]


def test_build_smoke_probe_command_host_vs_compose_web(tmp_path: Path):
    mgr = SlimDeployManager(project_root=tmp_path)
    host = mgr._build_smoke_probe_command("/api/health/", via_compose_web=False, quiet=True)
    assert "127.0.0.1:8000/api/health/" in host
    assert "docker compose" not in host
    inner = mgr._build_smoke_probe_command("/api/health/", via_compose_web=True, quiet=True)
    assert "docker compose" in inner
    assert "exec -T web" in inner


def test_build_merged_env_normalizes_schemed_domain(tmp_path: Path):
    repo = tmp_path / "repo"
    worktree = repo / "worktrees" / "demo"
    deploy_dir = tmp_path / "deploy"
    (worktree / ".ilha").mkdir(parents=True)
    (worktree / ".ilha" / "env.ilha").write_text("COMPOSE_PROJECT_NAME=demo\n")
    (deploy_dir / "code" / ".ilha").mkdir(parents=True)

    mgr = SlimDeployManager(project_root=repo)
    mgr._build_merged_env(
        deploy_dir=deploy_dir,
        worktree_path=worktree,
        branch_name="demo",
        domain="https://staging.example.com/login/",
    )

    text = (deploy_dir / ".env").read_text()
    assert 'SITE_DOMAIN="staging.example.com"' in text
    assert (
        'CSRF_TRUSTED_ORIGINS="https://staging.example.com,http://staging.example.com,https://*.example.com"'
        in text
    )
    assert 'CADDY_EMAIL="admin@staging.example.com"' in text


def test_run_smoke_checks_marks_web_container_probe_when_web_service_exists(tmp_path: Path, monkeypatch):
    mgr = SlimDeployManager(project_root=tmp_path)
    monkeypatch.setattr(mgr, "_remote_service_exists", lambda *a, **k: True)
    monkeypatch.setattr(mgr, "_run_remote_text", lambda *a, **k: (0, "", ""))
    monkeypatch.setattr("ilha.commands.push.slim_deploy_manager.time.sleep", lambda _seconds: None)

    ok, report = mgr._run_smoke_checks(
        username="root",
        server="1.2.3.4",
        smoke_path="/",
        domain=None,
        ip=None,
    )

    assert ok is True
    assert report["checks"][0]["probe"] == "web_container"


def test_wait_for_dns_resolution_returns_true_on_resolve(tmp_path: Path, monkeypatch):
    """Polling exits as soon as ``socket.gethostbyname`` returns an answer."""
    mgr = SlimDeployManager(project_root=tmp_path)

    import socket as _socket

    answers = iter([
        _socket.gaierror(8, "nodename nor servname provided"),
        _socket.gaierror(8, "nodename nor servname provided"),
        "203.0.113.7",
    ])

    def fake_gethostbyname(host: str) -> str:
        nxt = next(answers)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    monkeypatch.setattr("socket.gethostbyname", fake_gethostbyname)
    monkeypatch.setattr("ilha.commands.push.slim_deploy_manager.time.sleep", lambda _s: None)

    assert mgr._wait_for_dns_resolution("demo.example.com", expected_ip=None, timeout_seconds=5) is True


def test_wait_for_dns_resolution_returns_false_on_timeout(tmp_path: Path, monkeypatch):
    """Returns False when the deadline passes without a successful answer."""
    mgr = SlimDeployManager(project_root=tmp_path)

    def fake_gethostbyname(host: str) -> str:
        import socket as _socket
        raise _socket.gaierror(8, "nodename nor servname provided")

    monkeypatch.setattr("socket.gethostbyname", fake_gethostbyname)
    monkeypatch.setattr("ilha.commands.push.slim_deploy_manager.time.sleep", lambda _s: None)

    assert mgr._wait_for_dns_resolution("demo.example.com", expected_ip=None, timeout_seconds=0) is False


def test_build_merged_env_does_not_synthesize_database_url_for_static_site(tmp_path: Path):
    """Caddy-only worktrees must not get a phantom DATABASE_URL with bipassword."""
    repo = tmp_path / "repo"
    worktree = repo / "worktrees" / "demo"
    deploy_dir = tmp_path / "deploy"
    (worktree / ".ilha").mkdir(parents=True)
    (worktree / ".ilha" / "env.ilha").write_text("COMPOSE_PROJECT_NAME=demo\n")
    (worktree / ".ilha" / "docker-compose.overlay.yml").write_text(
        "services:\n  caddy:\n    image: caddy:2-alpine\n"
    )
    (deploy_dir / "code" / ".ilha").mkdir(parents=True)

    mgr = SlimDeployManager(project_root=repo)
    mgr._build_merged_env(
        deploy_dir=deploy_dir,
        worktree_path=worktree,
        branch_name="demo",
        domain="static.example.com",
    )

    text = (deploy_dir / ".env").read_text()
    assert "DATABASE_URL" not in text
    assert "bipassword" not in text


def test_build_merged_env_synthesizes_database_url_for_django_postgres(tmp_path: Path):
    """Postgres-bearing worktrees still get the DATABASE_URL fallback."""
    repo = tmp_path / "repo"
    worktree = repo / "worktrees" / "demo"
    deploy_dir = tmp_path / "deploy"
    (worktree / ".ilha").mkdir(parents=True)
    (worktree / ".ilha" / "env.ilha").write_text(
        "COMPOSE_PROJECT_NAME=demo\nPOSTGRES_PASSWORD=secret\nPOSTGRES_USER=u\nPOSTGRES_DB=app\n"
    )
    (worktree / ".ilha" / "docker-compose.overlay.yml").write_text(
        "services:\n  web:\n    image: app\n  db:\n    image: postgres:16\n"
    )
    (worktree / "manage.py").write_text("")
    (deploy_dir / "code" / ".ilha").mkdir(parents=True)

    mgr = SlimDeployManager(project_root=repo)
    mgr._build_merged_env(
        deploy_dir=deploy_dir,
        worktree_path=worktree,
        branch_name="demo",
        domain="app.example.com",
    )

    text = (deploy_dir / ".env").read_text()
    assert 'DATABASE_URL="postgres://u:secret@db:5432/app"' in text


def test_backup_volumes_locally_skips_when_no_volumes_on_host(tmp_path: Path, monkeypatch):
    """Static-site projects (no real volumes) must skip cleanly, not error."""
    mgr = SlimDeployManager(project_root=tmp_path)
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()

    monkeypatch.setattr(
        "ilha.utils.validation.validate_volume_exists",
        lambda _name: False,
    )

    backup_called = {"count": 0}

    class _StubDocker:
        def __init__(self, project_root):
            pass

        def backup_volumes(self, *args, **kwargs):
            backup_called["count"] += 1
            return None

    monkeypatch.setattr(
        "ilha.core.docker_manager.DockerManager", _StubDocker
    )

    result = mgr._backup_volumes_locally("demo", deploy_dir)

    assert result is None
    assert backup_called["count"] == 0, (
        "DockerManager.backup_volumes must NOT be invoked when zero volumes exist on the host"
    )


def test_retarget_paths_to_code_subdir_rewrites_build_volumes_env_file(tmp_path: Path):
    """Base compose lives one level above the code/ dir on the droplet, so
    user-written relative paths must be retargeted into ./code/."""
    mgr = SlimDeployManager(project_root=tmp_path)
    data = {
        "services": {
            "web": {
                "build": {"context": ".", "dockerfile": "Dockerfile"},
                "volumes": [".:/app", "/var/run/docker.sock:/var/run/docker.sock"],
                "env_file": [".env", "./config/extra.env"],
            },
            "frontend": {
                "build": "./frontend",
                "volumes": [
                    "./frontend:/app/frontend",
                    "frontend_node_modules:/app/frontend/node_modules",
                ],
            },
            "db": {
                "image": "postgres:16-alpine",
                "volumes": [{"type": "bind", "source": "./pgdata", "target": "/var/lib/postgresql/data"}],
            },
        }
    }

    mgr._retarget_paths_to_code_subdir(data)

    web = data["services"]["web"]
    assert web["build"]["context"] == "./code"
    assert web["build"]["dockerfile"] == "Dockerfile"  # left alone
    assert web["volumes"] == ["./code:/app", "/var/run/docker.sock:/var/run/docker.sock"]
    assert web["env_file"] == ["./code/.env", "./code/config/extra.env"]

    frontend = data["services"]["frontend"]
    assert frontend["build"] == "./code/frontend"
    assert frontend["volumes"] == [
        "./code/frontend:/app/frontend",
        "frontend_node_modules:/app/frontend/node_modules",  # named volume untouched
    ]

    db = data["services"]["db"]
    assert db["volumes"][0]["source"] == "./code/pgdata"


def test_retarget_relative_path_leaves_absolute_and_named_refs():
    assert SlimDeployManager._retarget_relative_path(SlimDeployManager, "/abs/path") == "/abs/path"
    assert SlimDeployManager._retarget_relative_path(SlimDeployManager, "~/foo") == "~/foo"
    assert SlimDeployManager._retarget_relative_path(SlimDeployManager, "$VAR/foo") == "$VAR/foo"
    # Bare relative names get the prefix too (caller is responsible for
    # only passing values that are unambiguously paths).
    assert SlimDeployManager._retarget_relative_path(SlimDeployManager, "named_thing") == "./code/named_thing"
    assert SlimDeployManager._retarget_relative_path(SlimDeployManager, ".") == "./code"
    assert SlimDeployManager._retarget_relative_path(SlimDeployManager, "./x") == "./code/x"
    assert SlimDeployManager._retarget_relative_path(SlimDeployManager, "../x") == "./code/../x"
    assert SlimDeployManager._retarget_relative_path(SlimDeployManager, ".env") == "./code/.env"


def test_maybe_pin_published_images_fails_without_manifest(tmp_path: Path):
    root = tmp_path / "proj"
    (root / ".ilha").mkdir(parents=True)
    (root / ".ilha" / "config.yml").write_text("project_name: demo\n")
    (root / "manage.py").write_text("")
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    (deploy_dir / "docker-compose.yml").write_text(
        "services:\n  web:\n    build: .\n    image: app\n  db:\n    image: postgres:16\n"
    )
    (deploy_dir / "docker-compose.overlay.yml").write_text("services: {}\n")
    (deploy_dir / ".env").write_text("COMPOSE_PROJECT_NAME=demo\n")
    mgr = SlimDeployManager(project_root=root)
    assert mgr._maybe_pin_published_images(deploy_dir, "staging", pin_published_images=True) is None


def test_maybe_pin_published_images_rewrites_when_manifest_exists(tmp_path: Path):
    root = tmp_path / "proj"
    ilha = root / ".ilha"
    ilha.mkdir(parents=True)
    (ilha / "config.yml").write_text("project_name: demo\n")
    (root / "manage.py").write_text("")
    (ilha / "images").mkdir()
    (ilha / "images" / "staging.json").write_text(
        json.dumps(
            {
                "branch": "staging",
                "services": [
                    {
                        "service": "web",
                        "image": "registry.digitalocean.com/reg/repo/web:tag",
                        "digest": "sha256:abc",
                    }
                ],
            }
        )
    )
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    (deploy_dir / "docker-compose.yml").write_text(
        "services:\n  web:\n    build: .\n    image: app\n  db:\n    image: postgres:16\n"
    )
    (deploy_dir / "docker-compose.overlay.yml").write_text("services: {}\n")
    (deploy_dir / ".env").write_text("COMPOSE_PROJECT_NAME=demo\n")
    mgr = SlimDeployManager(project_root=root)
    refs = mgr._maybe_pin_published_images(deploy_dir, "staging", pin_published_images=True)
    assert refs is not None
    assert refs["web"].endswith("@sha256:abc")
    text = (deploy_dir / "docker-compose.yml").read_text()
    assert "build:" not in text
    assert "sha256:abc" in text


# ── deploy-tree ownership (2026-08-21 staging outage regression) ─────────

def test_deploy_rsync_args_chown_to_app_user_never_laptop_uid():
    """-a implies -o -g: without --chown, root receiving rsync stamps the
    laptop's uid onto /app, leaving 0600 secrets unreadable by the container
    user. This took staging down on 2026-08-21."""
    args = SlimDeployManager.build_deploy_rsync_args(1000, 1000)
    assert "--chown=1000:1000" in args
    assert "-avz" in args  # modes still preserved; ownership is the override


def test_deploy_rsync_args_honor_configured_uid():
    args = SlimDeployManager.build_deploy_rsync_args(33, 33)
    assert "--chown=33:33" in args


def test_ownership_heal_targets_app_tree_but_never_volumes():
    """Heal covers files rsync skipped as unchanged; volumes/ keeps its own
    uid contract (postgres 999) and must never be swept."""
    cmd = SlimDeployManager.build_ownership_heal_command(1000, 1000)
    root = SlimDeployManager.REMOTE_APP_ROOT
    assert f"chown -R 1000:1000" in cmd
    for target in (f"{root}/code", f"{root}/.env", f"{root}/docker-compose.yml"):
        assert target in cmd
    assert "volumes" not in cmd
