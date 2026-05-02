"""Tests for ilha.core.worker_configurator."""

import yaml
from pathlib import Path

import pytest

from ilha.core.worker_configurator import WorkerConfigurator


@pytest.fixture
def wc():
    return WorkerConfigurator()


# ── _detect_service_ports ────────────────────────────────────────────


class TestDetectServicePorts:
    def test_returns_expose_ports(self, wc):
        compose = {
            "services": {
                "redis": {"expose": ["6379"]},
            }
        }
        assert wc._detect_service_ports(compose, "redis") == [6379]

    def test_ignores_ports_mappings(self, wc):
        compose = {
            "services": {
                "web": {"ports": ["8000:8000"]},
            }
        }
        assert wc._detect_service_ports(compose, "web") == []

    def test_deduplicates(self, wc):
        compose = {
            "services": {
                "redis": {"expose": ["6379", "6379"]},
            }
        }
        assert wc._detect_service_ports(compose, "redis") == [6379]

    def test_missing_service_returns_empty(self, wc):
        assert wc._detect_service_ports({"services": {}}, "nope") == []

    def test_invalid_port_skipped(self, wc):
        compose = {
            "services": {
                "redis": {"expose": ["abc", "6379"]},
            }
        }
        assert wc._detect_service_ports(compose, "redis") == [6379]


# ── _find_env_vars_for_service ───────────────────────────────────────


class TestFindEnvVarsForService:
    def test_finds_host_pattern_in_dict_env(self, wc):
        compose = {
            "services": {
                "web": {
                    "environment": {
                        "DB_HOST": "db:5432",
                        "REDIS_URL": "redis://redis:6379",
                    }
                }
            }
        }
        found = wc._find_env_vars_for_service(compose, "db")
        assert "DB_HOST" in found

    def test_finds_value_reference_in_dict_env(self, wc):
        compose = {
            "services": {
                "web": {
                    "environment": {
                        "DATABASE_URL": "postgres://user:pass@db:5432/mydb",
                    }
                }
            }
        }
        found = wc._find_env_vars_for_service(compose, "db")
        assert "DATABASE_URL" in found

    def test_finds_in_list_env_format(self, wc):
        compose = {
            "services": {
                "web": {
                    "environment": [
                        "DB_HOST=db:5432",
                        "REDIS_URL=redis://redis:6379",
                    ]
                }
            }
        }
        found = wc._find_env_vars_for_service(compose, "redis")
        assert "REDIS_URL" in found

    def test_returns_empty_for_no_matches(self, wc):
        compose = {
            "services": {
                "web": {
                    "environment": {"APP_NAME": "myapp"}
                }
            }
        }
        assert wc._find_env_vars_for_service(compose, "db") == []


# ── configure_vpc_port_bindings ──────────────────────────────────────


class TestConfigureVpcPortBindings:
    def test_disabled_without_config(self, wc, tmp_path):
        compose = {
            "services": {
                "redis": {"expose": ["6379"]},
            }
        }
        assert wc.configure_vpc_port_bindings(compose, ["redis"], tmp_path) is False
        assert "ports" not in compose["services"]["redis"]

    def test_binds_to_private_ip_when_enabled(self, wc, tmp_path):
        config_dir = tmp_path / ".ilha"
        config_dir.mkdir()
        (config_dir / "config.yml").write_text(
            yaml.dump({"vpc": {"auto_bind_ports": True, "bind_to_private_ip": True}})
        )
        compose = {
            "services": {
                "redis": {"expose": ["6379"]},
            }
        }
        result = wc.configure_vpc_port_bindings(compose, ["redis"], tmp_path, private_ip="10.0.0.5")
        assert result is True
        assert "10.0.0.5:6379:6379" in compose["services"]["redis"]["ports"]

    def test_falls_back_to_0000_without_private_ip(self, wc, tmp_path):
        config_dir = tmp_path / ".ilha"
        config_dir.mkdir()
        (config_dir / "config.yml").write_text(
            yaml.dump({"vpc": {"auto_bind_ports": True, "bind_to_private_ip": True}})
        )
        compose = {
            "services": {
                "redis": {"expose": ["6379"]},
            }
        }
        wc.configure_vpc_port_bindings(compose, ["redis"], tmp_path)
        assert "0.0.0.0:6379:6379" in compose["services"]["redis"]["ports"]

    def test_skips_service_with_existing_ports(self, wc, tmp_path):
        config_dir = tmp_path / ".ilha"
        config_dir.mkdir()
        (config_dir / "config.yml").write_text(
            yaml.dump({"vpc": {"auto_bind_ports": True}})
        )
        compose = {
            "services": {
                "redis": {"ports": ["6379:6379"], "expose": ["6379"]},
            }
        }
        result = wc.configure_vpc_port_bindings(compose, ["redis"], tmp_path)
        assert result is False

    def test_skips_unlisted_services(self, wc, tmp_path):
        config_dir = tmp_path / ".ilha"
        config_dir.mkdir()
        (config_dir / "config.yml").write_text(
            yaml.dump({"vpc": {"auto_bind_ports": True}})
        )
        compose = {
            "services": {
                "redis": {"expose": ["6379"]},
                "db": {"expose": ["5432"]},
            }
        }
        wc.configure_vpc_port_bindings(compose, ["redis"], tmp_path)
        assert "ports" in compose["services"]["redis"]
        assert "ports" not in compose["services"]["db"]


# ── filter_compose_services ──────────────────────────────────────────


class TestFilterComposeServices:
    def _write_compose(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.dump(data, default_flow_style=False))

    def test_filters_to_selected_services(self, wc, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"
        worktree = tmp_path / "my-branch"
        worktree.mkdir()
        self._write_compose(compose_file, {
            "services": {
                "web": {"image": "app"},
                "db": {"image": "postgres"},
                "redis": {"image": "redis"},
            },
            "volumes": {"pgdata": None},
        })
        container_filter = [{"worktree": "my-branch", "container": "web"}]
        result = wc.filter_compose_services(compose_file, container_filter, worktree)

        assert result is not None
        assert "web" in result["services"]
        assert "redis" not in result["services"]

    def test_excludes_deps_from_depends_on_list(self, wc, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"
        worktree = tmp_path / "my-branch"
        worktree.mkdir()
        self._write_compose(compose_file, {
            "services": {
                "web": {
                    "image": "app",
                    "depends_on": ["db", "redis"],
                },
                "db": {"image": "postgres"},
                "redis": {"image": "redis"},
            },
        })
        container_filter = [{"worktree": "my-branch", "container": "web"}]
        result = wc.filter_compose_services(
            compose_file, container_filter, worktree, exclude_deps=["db"]
        )

        assert result is not None
        web_deps = result["services"]["web"].get("depends_on", [])
        assert "db" not in web_deps

    def test_excludes_deps_from_depends_on_dict(self, wc, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"
        worktree = tmp_path / "my-branch"
        worktree.mkdir()
        self._write_compose(compose_file, {
            "services": {
                "web": {
                    "image": "app",
                    "depends_on": {
                        "db": {"condition": "service_healthy"},
                        "redis": {"condition": "service_started"},
                    },
                },
                "db": {"image": "postgres"},
                "redis": {"image": "redis"},
            },
        })
        container_filter = [{"worktree": "my-branch", "container": "web"}]
        result = wc.filter_compose_services(
            compose_file, container_filter, worktree, exclude_deps=["db"]
        )

        assert result is not None
        web_deps = result["services"]["web"].get("depends_on", {})
        assert "db" not in web_deps
        assert "redis" in web_deps

    def test_returns_none_for_no_matching_services(self, wc, tmp_path):
        compose_file = tmp_path / "docker-compose.yml"
        worktree = tmp_path / "my-branch"
        worktree.mkdir()
        self._write_compose(compose_file, {
            "services": {"web": {"image": "app"}},
        })
        container_filter = [{"worktree": "other-branch", "container": "web"}]
        result = wc.filter_compose_services(compose_file, container_filter, worktree)
        assert result is None


# ── configure_worker_environment ─────────────────────────────────────


class TestConfigureWorkerEnvironment:
    def test_replaces_service_hostname_with_central_ip(self, wc, tmp_path):
        ilha_dir = tmp_path / ".ilha"
        ilha_dir.mkdir()
        (ilha_dir / "docker-compose.overlay.yml").write_text(yaml.dump({
            "services": {
                "web": {
                    "environment": {
                        "DB_HOST": "db:5432",
                        "REDIS_URL": "redis://redis:6379",
                    }
                }
            }
        }))
        (ilha_dir / "env.ilha").write_text("DB_HOST=db:5432\nREDIS_URL=redis://redis:6379\n")

        metadata = {
            "vpc_deployment": {
                "is_worker": True,
                "central_server_private_ip": "10.116.0.13",
                "excluded_services": ["db", "redis"],
            }
        }
        result = wc.configure_worker_environment(tmp_path, metadata)
        assert result is True

        env_content = (ilha_dir / "env.ilha").read_text()
        assert "10.116.0.13:5432" in env_content
        assert "10.116.0.13:6379" in env_content

    def test_skips_when_not_worker(self, wc, tmp_path):
        metadata = {"vpc_deployment": {"is_worker": False}}
        assert wc.configure_worker_environment(tmp_path, metadata) is False

    def test_skips_when_no_vpc_deployment(self, wc, tmp_path):
        assert wc.configure_worker_environment(tmp_path, {}) is False

    def test_skips_when_missing_central_ip(self, wc, tmp_path):
        metadata = {
            "vpc_deployment": {
                "is_worker": True,
                "excluded_services": ["db"],
            }
        }
        assert wc.configure_worker_environment(tmp_path, metadata) is False

    def test_preserves_port_numbers(self, wc, tmp_path):
        ilha_dir = tmp_path / ".ilha"
        ilha_dir.mkdir()
        (ilha_dir / "docker-compose.overlay.yml").write_text(yaml.dump({
            "services": {
                "web": {
                    "environment": {
                        "DATABASE_URL": "postgres://user:pass@db:5432/mydb",
                    }
                }
            }
        }))
        (ilha_dir / "env.ilha").write_text(
            "DATABASE_URL=postgres://user:pass@db:5432/mydb\n"
        )

        metadata = {
            "vpc_deployment": {
                "is_worker": True,
                "central_server_private_ip": "10.0.0.1",
                "excluded_services": ["db"],
            }
        }
        wc.configure_worker_environment(tmp_path, metadata)

        env_content = (ilha_dir / "env.ilha").read_text()
        assert "postgres://user:pass@10.0.0.1:5432/mydb" in env_content
