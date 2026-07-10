"""Unit tests for the worktree check engine (ilha.core.worktree_checks).

Checks are pure over a pre-loaded WorktreeCheckContext, so every case is a
constructed context + assertion — no Docker, no real worktree.
"""

from pathlib import Path
from types import SimpleNamespace

from ilha.config.manifest import IlhaManifest, WebService
from ilha.core.worktree_checks import (
    CheckResult,
    WorktreeCheckContext,
    errors_only,
    has_errors,
    run_worktree_checks,
)


def fp(*, has_django=False, has_postgres=False):
    return SimpleNamespace(has_django=has_django, has_postgres=has_postgres)


def routable_overlay(service="app"):
    return {"services": {service: {"labels": {"caddy.proxy": "x.localhost"}}}}


def default_manifest():
    return IlhaManifest(project_name="proj", web=[WebService(service="app", port=8000)])


def ctx(**kw):
    base = dict(
        branch="feat",
        worktree_path=Path("/tmp/wt"),
        base_compose={"services": {"app": {"expose": ["8000"]}}},
        overlay_compose=routable_overlay(),
        merged_env={},
        footprint=fp(),
        manifest=default_manifest(),
    )
    base.update(kw)
    return WorktreeCheckContext(**base)


# --- worktree existence -----------------------------------------------------

def test_missing_worktree_short_circuits():
    results = run_worktree_checks(ctx(worktree_path=None), include_docker=False)
    assert [r.check_id for r in results] == ["worktree.exists"]
    assert has_errors(results)


# --- files ------------------------------------------------------------------

def test_missing_overlay_is_error():
    results = run_worktree_checks(ctx(overlay_compose=None), include_docker=False)
    assert any(r.check_id == "files.overlay" and r.severity == "error" for r in results)


def test_unparseable_overlay_is_error():
    results = run_worktree_checks(
        ctx(overlay_compose=None, overlay_error="bad yaml"), include_docker=False
    )
    assert any(r.check_id == "files.overlay" for r in results)


def test_missing_base_compose_is_error():
    results = run_worktree_checks(ctx(base_compose=None), include_docker=False)
    assert any(r.check_id == "files.base" and r.severity == "error" for r in results)


# --- routing (the headline check) -------------------------------------------

def test_routable_service_passes():
    results = run_worktree_checks(ctx(), include_docker=False)
    assert not any(r.check_id == "routing.routable" for r in results)


def test_no_routable_service_is_error():
    overlay = {"services": {"server": {"container_name": "x"}}}  # no caddy.proxy labels
    results = run_worktree_checks(ctx(overlay_compose=overlay), include_docker=False)
    routing = [r for r in results if r.check_id == "routing.routable"]
    assert routing and routing[0].severity == "error"
    assert "caddy.proxy" in routing[0].remedy


def test_routing_check_skipped_when_overlay_missing():
    # files.overlay covers a missing overlay; don't double-report as unroutable.
    results = run_worktree_checks(ctx(overlay_compose=None), include_docker=False)
    assert not any(r.check_id == "routing.routable" for r in results)


def test_inferred_port_is_warning():
    # Routable service exists, but base declares neither expose nor ports.
    results = run_worktree_checks(
        ctx(base_compose={"services": {"app": {}}}), include_docker=False
    )
    port = [r for r in results if r.check_id == "routing.port"]
    assert port and port[0].severity == "warning"


def test_declared_port_no_warning():
    results = run_worktree_checks(
        ctx(base_compose={"services": {"app": {"ports": ["8000:8000"]}}}),
        include_docker=False,
    )
    assert not any(r.check_id == "routing.port" for r in results)


# --- env --------------------------------------------------------------------

def test_missing_required_env_is_error():
    results = run_worktree_checks(
        ctx(footprint=fp(has_postgres=True), merged_env={}), include_docker=False
    )
    env_errs = {r.check_id for r in results if r.check_id.startswith("env.")}
    assert "env.postgres_user" in env_errs


def test_present_env_passes():
    env = {"POSTGRES_USER": "u", "POSTGRES_PASSWORD": "p", "POSTGRES_DB": "d"}
    results = run_worktree_checks(
        ctx(footprint=fp(has_postgres=True), merged_env=env), include_docker=False
    )
    assert not any(r.check_id.startswith("env.") for r in results)


def test_insecure_secret_key_is_error():
    from ilha.config.settings import INSECURE_DEFAULT_SECRET_KEY

    results = run_worktree_checks(
        ctx(footprint=fp(has_django=True), merged_env={"DJANGO_SECRET_KEY": INSECURE_DEFAULT_SECRET_KEY}),
        include_docker=False,
    )
    assert any(r.check_id == "env.django_secret_key" and r.severity == "error" for r in results)


def test_env_read_error_is_reported():
    results = run_worktree_checks(ctx(env_error="permission denied"), include_docker=False)
    assert any(r.check_id == "env.readable" and r.severity == "error" for r in results)


# --- conflicts --------------------------------------------------------------

def test_conflicting_caddy_service_is_warning():
    base = {"services": {"app": {"expose": ["8000"]}, "caddy": {}, "web": {}}}
    results = run_worktree_checks(ctx(base_compose=base), include_docker=False)
    conflicts = [r for r in results if r.check_id == "compose.conflicts"]
    assert conflicts and conflicts[0].severity == "warning"


# --- manifest ---------------------------------------------------------------

def test_absent_manifest_is_warning():
    results = run_worktree_checks(ctx(manifest=None), include_docker=False)
    m = [r for r in results if r.check_id == "manifest.exists"]
    assert m and m[0].severity == "warning"


def test_invalid_manifest_is_error():
    results = run_worktree_checks(
        ctx(manifest=None, manifest_error="extra fields not permitted"),
        include_docker=False,
    )
    assert any(r.check_id == "manifest.valid" and r.severity == "error" for r in results)


def test_manifest_drift_is_error():
    # Manifest names a web service the compose no longer has.
    manifest = IlhaManifest(project_name="proj", web=[WebService(service="gone", port=8000)])
    results = run_worktree_checks(
        ctx(manifest=manifest, base_compose={"services": {"app": {"expose": ["8000"]}}}),
        include_docker=False,
    )
    drift = [r for r in results if r.check_id == "manifest.drift"]
    assert drift and drift[0].severity == "error"


def test_manifest_in_sync_no_drift():
    results = run_worktree_checks(ctx(), include_docker=False)
    assert not any(r.check_id.startswith("manifest.") for r in results)


# --- aggregation helpers ----------------------------------------------------

def test_has_errors_and_errors_only():
    results = [
        CheckResult("a", "warning", "w"),
        CheckResult("b", "error", "e"),
        CheckResult("c", "ok", "o"),
    ]
    assert has_errors(results)
    assert [r.check_id for r in errors_only(results)] == ["b"]


def test_clean_worktree_has_no_findings():
    # Fully valid Django+Postgres worktree yields an empty (healthy) report.
    env = {
        "POSTGRES_USER": "u",
        "POSTGRES_PASSWORD": "p",
        "POSTGRES_DB": "d",
        "DJANGO_SECRET_KEY": "unique-strong-key",
    }
    results = run_worktree_checks(
        ctx(footprint=fp(has_django=True, has_postgres=True), merged_env=env),
        include_docker=False,
    )
    assert results == []
    assert not has_errors(results)
