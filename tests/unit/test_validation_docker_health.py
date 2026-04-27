"""Tests for bounded Docker health probes (avoid CLI hangs on wedged daemon)."""

import subprocess

import pytest

from ilha.utils.validation import (
    DEFAULT_DOCKER_HEALTH_TIMEOUT_SECONDS,
    validate_docker_compose,
    validate_docker_running,
)


def test_validate_docker_running_passes_timeout_to_subprocess(monkeypatch):
    seen = {}

    def fake_run(cmd, **_kwargs):
        seen["cmd"] = cmd
        seen["timeout"] = _kwargs.get("timeout")

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr("ilha.utils.validation.subprocess.run", fake_run)
    assert validate_docker_running(timeout_seconds=7) is True
    assert seen["cmd"] == ["docker", "info"]
    assert seen["timeout"] == 7


def test_validate_docker_running_false_on_timeout(monkeypatch):
    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=_kwargs.get("timeout", 0))

    monkeypatch.setattr("ilha.utils.validation.subprocess.run", fake_run)
    assert validate_docker_running(timeout_seconds=1) is False


def test_validate_docker_compose_false_on_timeout(monkeypatch):
    calls = {"n": 0}

    def fake_run(cmd, **_kwargs):
        calls["n"] += 1
        if "compose" in cmd:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=_kwargs.get("timeout", 0))
        raise AssertionError("unexpected command")

    monkeypatch.setattr("ilha.utils.validation.subprocess.run", fake_run)
    assert validate_docker_compose(timeout_seconds=2) is False
    assert calls["n"] == 1


def test_default_docker_health_timeout_is_positive():
    assert DEFAULT_DOCKER_HEALTH_TIMEOUT_SECONDS > 0
