"""Tests for worker compose filtering and VPC bind safety."""

from __future__ import annotations

from pathlib import Path

import yaml

from ilha.core.worker_compose import WorkerComposeFilter


def _compose(tmp_path: Path) -> Path:
    path = tmp_path / "docker-compose.yml"
    path.write_text(
        yaml.safe_dump({"services": {"rq-worker-1": {"image": "x"}, "web": {"image": "y"}}})
    )
    return path


def test_filter_matches_branch_argument_when_directory_name_differs(tmp_path: Path):
    compose = _compose(tmp_path)
    worktree = tmp_path / "staging-17a5ac4"
    worktree.mkdir()
    filtered = WorkerComposeFilter().filter_compose_services(
        compose,
        container_filter=[{"worktree": "staging", "container": "rq-worker-1"}],
        worktree_path=worktree,
        branch_name="staging",
    )
    assert filtered is not None
    assert list(filtered["services"]) == ["rq-worker-1"]


def test_filter_does_not_match_unrelated_directory_without_branch(tmp_path: Path):
    compose = _compose(tmp_path)
    worktree = tmp_path / "staging-17a5ac4"
    worktree.mkdir()
    filtered = WorkerComposeFilter().filter_compose_services(
        compose,
        container_filter=[{"worktree": "staging", "container": "rq-worker-1"}],
        worktree_path=worktree,
    )
    assert filtered is None


def test_vpc_bind_refuses_public_fallback(tmp_path: Path):
    worktree = tmp_path / "wt"
    (worktree / ".ilha").mkdir(parents=True)
    (worktree / ".ilha" / "config.yml").write_text(
        "vpc:\n  auto_bind_ports: true\n  bind_to_private_ip: true\n"
    )
    compose = {"services": {"db": {"expose": [5432]}}}
    changed = WorkerComposeFilter().configure_vpc_port_bindings(
        compose, ["db"], worktree, private_ip=None
    )
    assert changed is False
    assert "ports" not in compose["services"]["db"]


def test_vpc_bind_uses_private_ip(tmp_path: Path):
    worktree = tmp_path / "wt"
    (worktree / ".ilha").mkdir(parents=True)
    (worktree / ".ilha" / "config.yml").write_text(
        "vpc:\n  auto_bind_ports: true\n  bind_to_private_ip: true\n"
    )
    compose = {"services": {"db": {"expose": [5432]}}}
    changed = WorkerComposeFilter().configure_vpc_port_bindings(
        compose, ["db"], worktree, private_ip="10.0.0.5"
    )
    assert changed is True
    assert compose["services"]["db"]["ports"] == ["10.0.0.5:5432:5432"]
    assert not any("0.0.0.0" in str(p) for p in compose["services"]["db"]["ports"])
