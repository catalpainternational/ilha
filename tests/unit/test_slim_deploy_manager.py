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
    (worktree / ".ilha" / "docker-compose.worktree.yml").write_text("services:\n  web:\n    image: app\n")
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
    assert manifest["git_dirty"] is True
    assert manifest["checksums"]["Dockerfile"] is not None
    assert manifest["checksums"][".ilha/docker-compose.worktree.yml"] is not None
    assert manifest["checksums"]["pyproject.toml"] is not None
    assert manifest["checksums"]["uv.lock"] is not None


def test_build_drift_report_warns_for_dockerfile_and_fails_for_compose(tmp_path: Path):
    root = tmp_path / "repo"
    worktree = root / "worktrees" / "demo"
    (root / ".ilha").mkdir(parents=True)
    (worktree / ".ilha").mkdir(parents=True)
    (root / "Dockerfile").write_text("FROM python:3.12\n")
    (worktree / "Dockerfile").write_text("FROM python:3.13\n")
    (root / ".ilha" / "docker-compose.worktree.yml").write_text("services:\n  web:\n    image: root\n")
    (worktree / ".ilha" / "docker-compose.worktree.yml").write_text("services:\n  web:\n    image: worktree\n")

    mgr = SlimDeployManager(project_root=root)
    report = mgr.preflight.build_drift_report(worktree, root)

    assert report["status"] == "error"
    severities = {entry["file"]: entry["severity"] for entry in report["differences"]}
    assert severities["Dockerfile"] == "warning"
    assert severities[".ilha/docker-compose.worktree.yml"] == "error"


def test_build_drift_report_skips_cross_tree_drift_when_heads_differ(tmp_path: Path, monkeypatch):
    root = tmp_path / "repo"
    worktree = root / "worktrees" / "demo"
    (root / ".ilha").mkdir(parents=True)
    (worktree / ".ilha").mkdir(parents=True)
    (root / ".ilha" / "docker-compose.worktree.yml").write_text("services:\n  web:\n    image: root\n")
    (worktree / ".ilha" / "docker-compose.worktree.yml").write_text("services:\n  web:\n    image: worktree\n")

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
    (root / ".ilha" / "docker-compose.worktree.yml").write_text("services:\n  web:\n    image: root\n")
    (worktree / ".ilha" / "docker-compose.worktree.yml").write_text("services:\n  web:\n    image: worktree\n")

    mgr = SlimDeployManager(project_root=root)
    monkeypatch.setattr(mgr.preflight, "_git_rev_parse_head", lambda _path: "same-head")

    report = mgr.preflight.build_drift_report(worktree, root, strict_deploy_drift=True)

    assert report["status"] == "error"
    assert report["skipped"] is False
    assert any(entry["file"] == ".ilha/docker-compose.worktree.yml" for entry in report["differences"])


def test_run_deploy_preflight_fails_on_critical_drift(tmp_path: Path, monkeypatch):
    root = tmp_path / "repo"
    worktree = root / "worktrees" / "demo"
    (root / ".ilha").mkdir(parents=True)
    (worktree / ".ilha").mkdir(parents=True)
    (root / ".ilha" / "docker-compose.worktree.yml").write_text("services:\n  web:\n    image: root\n")
    (worktree / ".ilha" / "docker-compose.worktree.yml").write_text("services:\n  web:\n    image: worktree\n")

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
    compose = worktree / ".ilha" / "docker-compose.worktree.yml"
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


def test_detect_env_contract_issues_warns_on_schemed_site_domain(tmp_path: Path):
    root = tmp_path / "repo"
    worktree = root / "worktrees" / "demo"
    (worktree / ".ilha").mkdir(parents=True)
    (worktree / ".ilha" / "env.ilha").write_text("SITE_DOMAIN=https://staging.example.com/login/\n")

    mgr = SlimDeployManager(project_root=root)
    warnings = mgr.preflight.detect_env_contract_issues(worktree)

    assert len(warnings) == 1
    assert warnings[0]["variable"] == "SITE_DOMAIN"
    assert warnings[0]["normalized_value"] == "staging.example.com"


def test_run_smoke_checks_retries_until_success(tmp_path: Path, monkeypatch):
    mgr = SlimDeployManager(project_root=tmp_path)
    calls = {"remote": 0, "local": 0}

    def fake_remote(*args, **kwargs):
        calls["remote"] += 1
        if calls["remote"] < 3:
            return 1, "", "connection refused"
        return 0, "", ""

    def fake_local(*args, **kwargs):
        calls["local"] += 1
        return 0, "", ""

    monkeypatch.setattr(mgr, "_remote_service_exists", lambda *a, **k: False)
    monkeypatch.setattr(mgr, "_run_remote_text", fake_remote)
    monkeypatch.setattr(mgr, "_run_local_text", fake_local)
    monkeypatch.setattr("ilha.commands.push.slim_deploy_manager.time.sleep", lambda _seconds: None)

    ok, report = mgr._run_smoke_checks(
        username="root",
        server="1.2.3.4",
        smoke_path="/api/health/",
        domain="demo.example.com",
        ip=None,
    )

    assert ok is True
    assert report["success"] is True
    assert calls["remote"] == 3
    assert calls["local"] == 1


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


def test_doctor_skips_missing_worker_service(tmp_path: Path, monkeypatch):
    mgr = SlimDeployManager(project_root=tmp_path)
    monkeypatch.setattr(
        mgr,
        "_run_deploy_preflight",
        lambda _branch: {
            "smoke_path": "/api/health/",
            "manifest": {"branch_name": "demo"},
            "drift_report": {"status": "ok", "differences": []},
        },
    )
    mgr._last_report["source_manifest"] = {"branch_name": "demo", "source_path": "/tmp/demo"}
    mgr._last_report["drift_report"] = {"status": "ok", "differences": []}
    monkeypatch.setattr(mgr.doctor_mgr, "_remote_service_exists", lambda _u, _s, service: service == "web")
    monkeypatch.setattr(
        mgr.doctor_mgr,
        "_doctor_collect_command",
        lambda title, command, **kwargs: {
            "title": title,
            "command": command,
            "success": True,
            "stdout": "ok",
            "stderr": "",
        },
    )

    report = mgr.doctor(branch_name="demo", scp_target="root@1.2.3.4:/root", domain="demo.example.com")
    skipped = [check for check in report["checks"] if check.get("skipped")]
    assert report["success"] is True
    assert report["smoke_path"] == "/api/health/"
    assert any(check["title"] == "rq-worker-1_logs" for check in skipped)
