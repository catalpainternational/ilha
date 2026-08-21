"""Unit tests for ephemeral worker bundle generation and manifest helpers."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml

from ilha.commands.image import ImageManager
from ilha.commands.worker import WorkerManager
from ilha.exceptions import ilhaCommandError


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    ilha = root / ".ilha"
    ilha.mkdir()
    (ilha / "config.yml").write_text(
        yaml.safe_dump(
            {
                "project_name": "demo",
                "worker_pool": {"worker_service": "rq-worker-1", "central_droplet_name": "central"},
            },
            sort_keys=False,
        )
    )
    worktree = root / "worktrees" / "staging"
    worktree.mkdir(parents=True)
    (worktree / ".ilha").mkdir()
    (worktree / ".ilha" / "env.ilha").write_text(
        "COMPOSE_PROJECT_NAME=demo-staging\n"
        "DB_HOST=db\n"
        "REDIS_HOST=redis\n"
        "DIGITALOCEAN_API_TOKEN=super-secret\n"
        "EMAIL_HOST_PASSWORD=also-secret\n"
    )
    compose = {
        "services": {
            "web": {"build": ".", "depends_on": ["db", "redis"]},
            "db": {"image": "postgres:15"},
            "redis": {"image": "redis:7"},
            "rq-worker-1": {
                "build": ".",
                "command": "rq worker default",
                "depends_on": ["db", "redis"],
                "environment": {
                    "DB_HOST": "${DB_HOST:-db}",
                    "REDIS_HOST": "${REDIS_HOST:-redis}",
                },
            },
        }
    }
    (worktree / "docker-compose.yml").write_text(yaml.safe_dump(compose, sort_keys=False))
    manifest = {
        "branch": "staging",
        "services": [
            {
                "service": "rq-worker-1",
                "image": "registry.digitalocean.com/reg/repo/rq-worker-1:staging-abc1234",
                "digest": "sha256:deadbeef",
            }
        ],
    }
    (ilha / "images").mkdir()
    (ilha / "images" / "staging.json").write_text(json.dumps(manifest))
    return root


def test_load_manifest_and_image_ref(tmp_path):
    root = _project(tmp_path)
    mgr = ImageManager(root)
    manifest = mgr.load_manifest("staging")
    assert manifest["branch"] == "staging"
    ref = mgr.image_ref_for_service("staging", "rq-worker-1")
    assert ref.endswith("@sha256:deadbeef")
    assert "rq-worker-1" in ref


def test_load_manifest_missing_raises(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    (root / ".ilha").mkdir()
    with pytest.raises(ilhaCommandError, match="Image manifest not found"):
        ImageManager(root).load_manifest("staging")


def test_build_bundle_swaps_build_for_image_and_rewrites_env(tmp_path):
    root = _project(tmp_path)
    mgr = WorkerManager(root)
    bundle = mgr.build_bundle("staging", "10.0.0.5")

    assert bundle.worker_service == "rq-worker-1"
    assert bundle.image_ref.endswith("@sha256:deadbeef")
    service = bundle.compose_data["services"]["rq-worker-1"]
    assert "build" not in service
    assert service["image"] == bundle.image_ref
    assert "depends_on" not in service
    assert bundle.env_vars["DB_HOST"] == "10.0.0.5"
    assert bundle.env_vars["REDIS_HOST"] == "10.0.0.5"
    assert "DIGITALOCEAN_API_TOKEN" not in bundle.env_vars
    assert "EMAIL_HOST_PASSWORD" not in bundle.env_vars
    assert bundle.compose_path.exists()
    assert bundle.env_path.exists()
    assert oct(bundle.env_path.stat().st_mode & 0o777) == oct(0o600)


def test_build_bundle_rejects_control_plane_allowlist(tmp_path):
    root = _project(tmp_path)
    config_path = root / ".ilha" / "config.yml"
    config = yaml.safe_load(config_path.read_text())
    config.setdefault("worker_pool", {})["env_allowlist"] = ["DIGITALOCEAN_API_TOKEN"]
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    mgr = WorkerManager(root)
    with pytest.raises(ilhaCommandError, match="cannot include control-plane"):
        mgr.build_bundle("staging", "10.0.0.5")


def test_build_bundle_merges_sops_profile_env(tmp_path):
    root = _project(tmp_path)
    config = yaml.safe_load((root / ".ilha" / "config.yml").read_text())
    config.setdefault("worker_pool", {})["env_allowlist"] = ["POSTGRES_PASSWORD"]
    (root / ".ilha" / "config.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    mgr = WorkerManager(root)

    with patch(
        "ilha.commands.worker.SecretsManager.decrypt_profile",
        return_value={"POSTGRES_PASSWORD": "from-sops"},
    ):
        bundle = mgr.build_bundle("staging", "10.0.0.5", secrets_profile="staging")

    assert bundle.env_vars["POSTGRES_PASSWORD"] == "from-sops"


def test_render_cloud_init_omits_env_and_registry_token(tmp_path, monkeypatch):
    root = _project(tmp_path)
    mgr = WorkerManager(root)
    bundle = mgr.build_bundle("staging", "10.0.0.5")
    bundle.env_vars["POSTGRES_PASSWORD"] = "real-pw"
    bundle.env_path.write_text("POSTGRES_PASSWORD=real-pw\n")

    monkeypatch.setenv("DOCR_READ_TOKEN", "profile-token")
    cloud_init = mgr.render_cloud_init(bundle)

    assert "docker-ce" in cloud_init
    assert "REGISTRY_TOKEN" not in cloud_init
    assert "ENV_B64" not in cloud_init
    assert "profile-token" not in cloud_init
    assert "real-pw" not in cloud_init
    assert "super-secret" not in cloud_init
    assert "docker login" not in cloud_init
    assert "docker compose" not in cloud_init
    assert "{{COMPOSE_B64}}" not in cloud_init


def test_render_cloud_init_embeds_compose_only(tmp_path):
    root = _project(tmp_path)
    mgr = WorkerManager(root)
    bundle = mgr.build_bundle("staging", "10.0.0.5")
    cloud_init = mgr.render_cloud_init(bundle)
    import base64

    marker = "echo '"
    start = cloud_init.find(marker)
    assert start != -1
    b64 = cloud_init[start + len(marker) :].split("'", 1)[0]
    compose = base64.b64decode(b64).decode()
    assert "rq-worker-1" in compose
    assert bundle.image_ref in compose


def test_create_droplet_passes_user_data(monkeypatch):
    from ilha.commands.droplets import DropletCommands

    captured = {}

    class FakeProvider:
        def create_droplet(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                id=1,
                name=kwargs["name"],
                ip_address="1.2.3.4",
                status="active",
                region="nyc1",
                size="s-1vcpu-1gb",
                image="ubuntu-22-04-x64",
                tags=kwargs.get("tags") or [],
                private_ip_address="10.0.0.2",
                vpc_uuid="vpc-1",
            )

        def wait_for_droplet_ready(self, droplet_id, timeout=300, check_ssh=True):
            return True

        def get_droplet(self, droplet_id):
            return captured.get("_droplet") or SimpleNamespace(
                id=1,
                name="worker-staging-1",
                ip_address="1.2.3.4",
                status="active",
                region="nyc1",
                size="s-1vcpu-1gb",
                image="ubuntu-22-04-x64",
                tags=["worker"],
                private_ip_address="10.0.0.2",
                vpc_uuid="vpc-1",
            )

    monkeypatch.setattr(
        "ilha.commands.droplets.DropletManager.resolve_droplet_token",
        lambda api_token: "token",
    )
    monkeypatch.setattr(
        "ilha.commands.droplets.DropletManager.create_provider",
        lambda provider, token: FakeProvider(),
    )
    monkeypatch.setattr(
        "ilha.commands.droplets.DropletManager.get_droplet_defaults",
        lambda: {"region": "nyc1", "size": "s-1vcpu-1gb", "image": "ubuntu-22-04-x64", "ssh_keys": []},
    )

    cmds = DropletCommands()
    result = cmds.create_droplet(
        name="worker-staging-1",
        user_data="#cloud-config\nruncmd: echo hi",
        return_droplet=True,
        json=True,
    )
    assert result is not None
    assert captured.get("user_data") == "#cloud-config\nruncmd: echo hi"


def test_list_droplets_by_tag_raises_on_provider_error(monkeypatch):
    from ilha.commands.droplets import DropletCommands
    from ilha.exceptions import ilhaCommandError

    monkeypatch.setattr(
        "ilha.commands.droplets.DropletCommands._get_provider",
        lambda self, api_token, json=False: (None, "bad token"),
    )

    with pytest.raises(ilhaCommandError, match="bad token"):
        DropletCommands().list_droplets_by_tag("worker")


def test_list_droplets_by_tag_filters_branch(monkeypatch):
    from ilha.commands.droplets import DropletCommands
    from ilha.core.droplet_manager import DropletInfo

    droplets = [
        DropletInfo(
            id=1,
            name="worker-staging-1",
            ip_address="1.2.3.4",
            status="active",
            region="nyc1",
            size="s-1vcpu-1gb",
            image="ubuntu",
            tags=["worker", "branch:staging"],
        ),
        DropletInfo(
            id=2,
            name="worker-main-1",
            ip_address="5.6.7.8",
            status="active",
            region="nyc1",
            size="s-1vcpu-1gb",
            image="ubuntu",
            tags=["worker", "branch:main"],
        ),
    ]

    class FakeProvider:
        def list_droplets(self):
            return droplets

    monkeypatch.setattr(
        "ilha.commands.droplets.DropletManager.resolve_droplet_token",
        lambda api_token: "token",
    )
    monkeypatch.setattr(
        "ilha.commands.droplets.DropletManager.create_provider",
        lambda provider, token: FakeProvider(),
    )

    results = DropletCommands().list_droplets_by_tag("worker", branch="staging")
    assert [d.id for d in results] == [1]


def test_filter_strips_sensitive_keys_even_with_allowlist(tmp_path):
    """An allowlist opts specific keys in; it must not disable the filter."""
    root = _project(tmp_path)
    config = yaml.safe_load((root / ".ilha" / "config.yml").read_text())
    config.setdefault("worker_pool", {})["env_allowlist"] = ["POSTGRES_PASSWORD"]
    (root / ".ilha" / "config.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    # Worker service references both a sensitive and a plain variable.
    worktree = root / "worktrees" / "staging"
    compose = yaml.safe_load((worktree / "docker-compose.yml").read_text())
    compose["services"]["rq-worker-1"]["environment"].update(
        {
            "LLM_API_KEY": "${LLM_API_KEY:-}",
            "SAVE_ARTIFACTS": "${SAVE_ARTIFACTS:-False}",
        }
    )
    (worktree / "docker-compose.yml").write_text(yaml.safe_dump(compose, sort_keys=False))
    env_file = worktree / ".ilha" / "env.ilha"
    env_file.write_text(
        env_file.read_text()
        + "POSTGRES_PASSWORD=real-pw\nLLM_API_KEY=sk-123\nSAVE_ARTIFACTS=True\n"
    )

    bundle = WorkerManager(root).build_bundle("staging", "10.0.0.5")

    assert bundle.env_vars["POSTGRES_PASSWORD"] == "real-pw"  # allowlisted opt-in
    assert "LLM_API_KEY" not in bundle.env_vars  # sensitive, not allowlisted
    assert bundle.env_vars["SAVE_ARTIFACTS"] == "True"  # plain key survives


def test_env_overrides_pin_bundle_env(tmp_path):
    """worker_pool.env_overrides wins over central env and compose defaults."""
    root = _project(tmp_path)
    config = yaml.safe_load((root / ".ilha" / "config.yml").read_text())
    config.setdefault("worker_pool", {})["env_overrides"] = {"RQ_QUEUE_NAMES": "scraping"}
    (root / ".ilha" / "config.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    worktree = root / "worktrees" / "staging"
    compose = yaml.safe_load((worktree / "docker-compose.yml").read_text())
    compose["services"]["rq-worker-1"]["environment"]["RQ_QUEUE_NAMES"] = (
        "${RQ_QUEUE_NAMES:-default,scraping,onboarding}"
    )
    (worktree / "docker-compose.yml").write_text(yaml.safe_dump(compose, sort_keys=False))
    env_file = worktree / ".ilha" / "env.ilha"
    env_file.write_text(env_file.read_text() + "RQ_QUEUE_NAMES=default,scraping,onboarding\n")

    bundle = WorkerManager(root).build_bundle("staging", "10.0.0.5")

    assert bundle.env_vars["RQ_QUEUE_NAMES"] == "scraping"
    assert "RQ_QUEUE_NAMES=scraping" in bundle.env_path.read_text()


def test_env_overrides_must_be_a_mapping(tmp_path):
    root = _project(tmp_path)
    config = yaml.safe_load((root / ".ilha" / "config.yml").read_text())
    config.setdefault("worker_pool", {})["env_overrides"] = ["RQ_QUEUE_NAMES=scraping"]
    (root / ".ilha" / "config.yml").write_text(yaml.safe_dump(config, sort_keys=False))
    with pytest.raises(ilhaCommandError, match="env_overrides"):
        WorkerManager(root).load_pool_config()


def test_rewrite_env_never_fabricates_database_url(tmp_path):
    root = _project(tmp_path)
    mgr = WorkerManager(root)
    env, _ = mgr._rewrite_env_for_central({"DB_HOST": "db"}, {"services": {}}, "10.0.0.5", [])
    assert "DATABASE_URL" not in env  # no invented credentials
    assert env["REDIS_URL"] == "redis://10.0.0.5:6379/0"

    full, _ = mgr._rewrite_env_for_central(
        {"POSTGRES_USER": "u", "POSTGRES_PASSWORD": "p", "POSTGRES_DB": "d"},
        {"services": {}},
        "10.0.0.5",
        [],
    )
    assert full["DATABASE_URL"] == "postgres://u:p@10.0.0.5:5432/d"


def test_rewrite_env_does_not_rewrite_a_uri_scheme(tmp_path):
    """`redis://` is a scheme, not the redis host — only the host redirects."""
    root = _project(tmp_path)
    mgr = WorkerManager(root)
    compose = {
        "services": {
            "rq-worker-1": {
                "environment": {
                    "REDIS_URL": "${REDIS_URL:-redis://${REDIS_HOST:-redis}:${REDIS_PORT:-6379}/${REDIS_DB:-0}}",
                }
            }
        }
    }
    _, out = mgr._rewrite_env_for_central({}, compose, "10.0.0.5", ["redis"])
    value = out["services"]["rq-worker-1"]["environment"]["REDIS_URL"]
    assert value == "${REDIS_URL:-redis://${REDIS_HOST:-10.0.0.5}:${REDIS_PORT:-6379}/${REDIS_DB:-0}}"
    assert "10.0.0.5://" not in value  # the scheme must survive intact


def test_rewrite_env_applies_every_excluded_service_to_one_value(tmp_path):
    """Two services in one value: both redirect, neither overwrites the other."""
    root = _project(tmp_path)
    mgr = WorkerManager(root)
    compose = {
        "services": {
            "rq-worker-1": {
                "environment": {"UPSTREAMS": "db:5432,redis:6379"},
            }
        }
    }
    _, out = mgr._rewrite_env_for_central({}, compose, "10.0.0.5", ["db", "redis"])
    assert out["services"]["rq-worker-1"]["environment"]["UPSTREAMS"] == (
        "10.0.0.5:5432,10.0.0.5:6379"
    )


def test_next_worker_index_skips_past_live_names():
    assert WorkerManager.next_worker_index([]) == 1
    assert WorkerManager.next_worker_index(["worker-staging-1", "worker-staging-2"]) == 3
    # After worker-staging-1 was destroyed, len+1 would mint a duplicate "-2".
    assert WorkerManager.next_worker_index(["worker-staging-2"]) == 3
    assert WorkerManager.next_worker_index(["oddname"]) == 1


def test_worker_tags_include_project_scope(tmp_path):
    root = _project(tmp_path)
    tags = WorkerManager(root).worker_tags("staging")
    assert "worker" in tags
    assert "branch:staging" in tags
    # From config project_name, NOT the directory basename — the basename
    # differs between a laptop worktree and the autoscaler container.
    assert "ilha-project:demo" in tags


def test_list_droplets_by_tag_require_tags(monkeypatch):
    from ilha.commands.droplets import DropletCommands
    from ilha.core.droplet_manager import DropletInfo

    def info(id, tags):
        return DropletInfo(
            id=id, name=f"worker-{id}", ip_address=None, status="active",
            region="syd1", size="s", image="ubuntu", tags=tags,
        )

    droplets = [
        info(1, ["worker", "branch:staging", "ilha-project:proj"]),
        info(2, ["worker", "branch:staging", "ilha-project:other"]),
    ]

    class FakeProvider:
        def list_droplets(self):
            return droplets

    monkeypatch.setattr(
        "ilha.commands.droplets.DropletCommands._get_provider",
        lambda self, api_token, json=False: (FakeProvider(), None),
    )
    found = DropletCommands().list_droplets_by_tag(
        "worker", branch="staging", require_tags=["ilha-project:proj"]
    )
    assert [d.id for d in found] == [1]


def test_assert_worker_deploy_preflight_enforces_caps_and_ssh():
    WorkerManager.assert_worker_deploy_preflight(
        wait=True,
        ssh_keys=["fingerprint"],
        count=1,
        existing_count=9,
        max_workers=10,
        registry_token="tok",
    )
    with pytest.raises(ilhaCommandError, match="max_workers"):
        WorkerManager.assert_worker_deploy_preflight(
            wait=True,
            ssh_keys=["fingerprint"],
            count=1,
            existing_count=10,
            max_workers=10,
            registry_token="tok",
        )
    with pytest.raises(ilhaCommandError, match="SSH keys"):
        WorkerManager.assert_worker_deploy_preflight(
            wait=True,
            ssh_keys=[],
            count=1,
            existing_count=0,
            max_workers=10,
            registry_token="tok",
        )
    with pytest.raises(ilhaCommandError, match="wait"):
        WorkerManager.assert_worker_deploy_preflight(
            wait=False,
            ssh_keys=["fingerprint"],
            count=1,
            existing_count=0,
            max_workers=10,
        )
    with pytest.raises(ilhaCommandError, match="Registry token"):
        WorkerManager.assert_worker_deploy_preflight(
            wait=True,
            ssh_keys=["fingerprint"],
            count=1,
            existing_count=0,
            max_workers=10,
            registry_token="  ",
        )


def test_select_idle_droplets_skips_missing_created_at():
    from datetime import datetime, timedelta, timezone

    from ilha.core.droplet_manager import DropletInfo

    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

    def info(id, created_at):
        return DropletInfo(
            id=id,
            name=f"worker-{id}",
            ip_address="1.2.3.4",
            status="active",
            region="syd1",
            size="s",
            image="ubuntu",
            created_at=created_at,
        )

    old = info(1, now - timedelta(seconds=400))
    young = info(2, now - timedelta(seconds=10))
    unknown = info(3, None)
    selected = WorkerManager.select_idle_droplets(
        [old, young, unknown],
        idle_after_seconds=300,
        now=now,
    )
    assert [d.id for d in selected] == [1]


def test_bootstrap_worker_passes_token_on_stdin_not_argv(tmp_path):
    root = _project(tmp_path)
    mgr = WorkerManager(root)
    bundle = mgr.build_bundle("staging", "10.0.0.5")
    runs = []

    class FakeSSH:
        def ensure_host_key(self, host):
            return True

        def run_remote_text(self, username, host, command, timeout=30):
            if command.startswith("mkdir"):
                return 0, "", ""
            if command == "docker info":
                return 0, "", ""
            if command.startswith("chmod"):
                return 0, "", ""
            if "docker pull" in command:
                return 0, "", ""
            return 1, "", f"unexpected: {command}"

        def build_scp_command(self, source, target):
            return ["scp", source, target]

        def build_ssh_command(self, username, server, command=None):
            return ["ssh", f"{username}@{server}", command or ""]

    def fake_run(cmd, **kwargs):
        runs.append({"cmd": cmd, "input": kwargs.get("input")})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    mgr.bootstrap_worker(
        bundle,
        "203.0.113.9",
        registry_token="docr-secret-token",
        ssh=FakeSSH(),
        retries=1,
        sleep=lambda _s: None,
        run=fake_run,
    )

    scp_cmds = [r["cmd"] for r in runs if r["cmd"] and r["cmd"][0] == "scp"]
    assert any("env.ilha" in " ".join(c) for c in scp_cmds)
    login = next(r for r in runs if r["cmd"] and r["cmd"][0] == "ssh")
    assert login["input"] == "docr-secret-token\n"
    assert "docr-secret-token" not in " ".join(login["cmd"])
    joined_scp = " ".join(" ".join(c) for c in scp_cmds)
    assert "docr-secret-token" not in joined_scp



def test_project_scope_tag_prefers_config_project_name(tmp_path):
    """The same project is staging/ on a laptop and /app in the autoscaler
    container; basename tags split one fleet into two invisible halves."""
    from ilha.commands.worker import project_scope_tag

    root = tmp_path / "app"
    (root / ".ilha").mkdir(parents=True)
    (root / ".ilha" / "config.yml").write_text("project_name: business_intelligence\n")
    assert project_scope_tag(root) == "ilha-project:business_intelligence"


def test_project_scope_tag_falls_back_to_basename(tmp_path):
    from ilha.commands.worker import project_scope_tag

    bare = tmp_path / "some-dir"
    bare.mkdir()
    assert project_scope_tag(bare) == "ilha-project:some-dir"
