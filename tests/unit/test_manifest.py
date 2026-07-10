"""Unit tests for the ratified project manifest (ilha.config.manifest)."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ilha.config.manifest import (
    IlhaManifest,
    WebService,
    diff_manifest_vs_compose,
    load_manifest,
    manifest_from_detection,
    write_manifest,
)


def fp(*, has_django=False, has_postgres=False, db_service=None):
    return SimpleNamespace(
        has_django=has_django, has_postgres=has_postgres, db_service=db_service
    )


# --- model validation -------------------------------------------------------

def test_manifest_rejects_unknown_keys():
    with pytest.raises(ValidationError):
        IlhaManifest.model_validate({"project_name": "p", "wbe": []})  # typo'd key


def test_web_service_port_bounds():
    with pytest.raises(ValidationError):
        WebService(service="app", port=0)
    with pytest.raises(ValidationError):
        WebService(service="app", port=99999)


def test_routable_ports_map():
    m = IlhaManifest(
        project_name="p",
        web=[WebService(service="app", port=8000), WebService(service="api", port=3000)],
    )
    assert m.routable_ports() == {"app": 8000, "api": 3000}


# --- detection --------------------------------------------------------------

def test_detection_picks_conventional_web_service():
    compose = {"services": {"app": {"expose": ["8000"]}, "db": {"image": "postgres:16"}}}
    m = manifest_from_detection(compose, fp(has_django=True, has_postgres=True, db_service="db"), "proj")
    assert [w.service for w in m.web] == ["app"]
    assert m.web[0].port == 8000
    assert m.db.service == "db" and m.db.engine == "postgres"
    assert m.env.profile == "django"


def test_detection_non_django_is_generic():
    compose = {"services": {"web": {"expose": ["5000"]}}}
    m = manifest_from_detection(compose, fp(), "proj")
    assert m.env.profile == "generic"
    assert m.web[0].port == 5000


def test_detection_unroutable_project_has_empty_web():
    # A service named 'server' matches no heuristic → nothing routable proposed.
    compose = {"services": {"server": {"image": "myco/server"}}}
    m = manifest_from_detection(compose, fp(), "proj")
    assert m.web == []


def test_detection_excludes_external_volumes():
    compose = {
        "services": {"app": {"expose": ["8000"]}},
        "volumes": {"data": {}, "shared": {"external": True}},
    }
    m = manifest_from_detection(compose, fp(), "proj")
    assert m.volumes == ["data"]


# --- round-trip -------------------------------------------------------------

def test_write_then_load_roundtrip(tmp_path):
    m = manifest_from_detection(
        {"services": {"app": {"expose": ["8000"]}}}, fp(has_django=True), "proj"
    )
    write_manifest(tmp_path, m)
    loaded = load_manifest(tmp_path)
    assert loaded is not None
    assert loaded.project_name == "proj"
    assert loaded.routable_ports() == {"app": 8000}


def test_load_absent_manifest_returns_none(tmp_path):
    assert load_manifest(tmp_path) is None


def test_load_invalid_manifest_raises(tmp_path):
    from ilha.config.settings import ILHA_DIR

    path = tmp_path / ILHA_DIR / "ilha.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("project_name: p\nbogus_key: 1\n")
    with pytest.raises(ValidationError):
        load_manifest(tmp_path)


# --- drift ------------------------------------------------------------------

def test_diff_flags_missing_service():
    m = IlhaManifest(project_name="p", web=[WebService(service="app", port=8000)])
    drift = diff_manifest_vs_compose(m, {"services": {"other": {}}})
    assert drift and "app" in drift[0]


def test_diff_clean_when_in_sync():
    m = IlhaManifest(project_name="p", web=[WebService(service="app", port=8000)])
    assert diff_manifest_vs_compose(m, {"services": {"app": {}}}) == []
