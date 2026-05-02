"""Tests for the shared multi-strategy HTTP probe primitives."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load():
    """The ``@dataclass`` in ``http_probe`` resolves its module via
    ``sys.modules[cls.__module__]`` at decoration time. ``spec_from_file_location``
    doesn't register the module in ``sys.modules`` automatically, so do that
    explicitly before ``exec_module``."""
    import sys

    path = Path(__file__).resolve().parents[2] / "ilha" / "scripts" / "http_probe.py"
    spec = importlib.util.spec_from_file_location("ilha_http_probe_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ilha_http_probe_under_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def probe():
    return _load()


# ── normalize_path ────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("/admin/login/", "/admin/login/"),
    ("admin/login/", "/admin/login/"),  # missing leading slash
    ("/", "/"),
    ("", "/"),
    (None, "/"),
    ("   /api/   ", "/api/"),  # surrounding whitespace stripped
    ("api/", "/api/"),
])
def test_normalize_path(probe, raw, expected):
    assert probe.normalize_path(raw) == expected


# ── ProbeStep / build_probe_steps ─────────────────────────────────────────


def test_default_strategies_emitted_in_order(probe):
    steps = probe.build_probe_steps(port=8000, path="/health/")
    assert [s.name for s in steps] == list(probe.DEFAULT_STRATEGIES)


def test_each_step_has_exists_check_and_probe_command(probe):
    for step in probe.build_probe_steps(port=8000, path="/"):
        assert step.exists_check  # non-empty (``:`` for tcp is intentional)
        assert step.probe_command


def test_python_probe_uses_http_client_no_redirect(probe):
    """Regression: Django apps behind ``SECURE_PROXY_SSL_HEADER`` 301 to HTTPS
    on in-container HTTP. ``http.client`` reads the raw status without
    following the redirect; ``urlopen`` would chase to a TLS-less https://
    listener and fail."""
    steps = probe.build_probe_steps(port=8000, path="/health/")
    py_step = next(s for s in steps if s.name == "python")
    assert "http.client" in py_step.probe_command
    # Acceptable status range covers 2xx and 3xx.
    assert "200 <= status < 400" in py_step.probe_command


def test_python_probe_picks_python3_or_python(probe):
    """Slim images may symlink only ``python3``; some only ``python``."""
    steps = probe.build_probe_steps(port=8000, path="/")
    py_step = next(s for s in steps if s.name == "python")
    assert "command -v python3 || command -v python" in py_step.exists_check


def test_curl_uses_fail_flag(probe):
    """``curl -f`` exits non-zero on >=400 — that's how ilha detects
    application-level failures via this strategy."""
    steps = probe.build_probe_steps(port=8000, path="/")
    curl_step = next(s for s in steps if s.name == "curl")
    assert "curl -fsS" in curl_step.probe_command


def test_path_with_special_chars_quoted_in_url(probe):
    """``shlex.quote`` should escape paths so they survive ``sh -c``."""
    steps = probe.build_probe_steps(port=8000, path="/admin login/")
    curl_step = next(s for s in steps if s.name == "curl")
    # The URL appears quoted (single quotes around the URL token).
    assert "'http://127.0.0.1:8000/admin login/'" in curl_step.probe_command


def test_strategy_subset_honored(probe):
    steps = probe.build_probe_steps(port=80, path="/", strategies=("curl", "python"))
    assert [s.name for s in steps] == ["curl", "python"]


def test_unknown_strategy_raises(probe):
    with pytest.raises(ValueError, match="Unknown probe strategy"):
        probe.build_probe_steps(port=80, path="/", strategies=("rsync",))


# ── standalone() — used by deploy_utils.health_check_http ─────────────────


def test_standalone_chains_exists_and_probe_with_short_circuit(probe):
    step = probe.build_probe_steps(port=8000, path="/")[0]
    assert step.standalone() == f"{step.exists_check} && {step.probe_command}"


# ── build_probe_shell_chain — used by smoke probes ────────────────────────


def test_shell_chain_uses_if_elif_else_in_strategy_order(probe):
    chain = probe.build_probe_shell_chain(port=8000, path="/")
    # First strategy uses ``if``, subsequent use ``elif``, terminating ``fi``.
    assert chain.startswith("if command -v curl >/dev/null 2>&1; then")
    assert " elif command -v wget >/dev/null 2>&1; then" in chain
    assert ' elif PY="$(command -v python3 || command -v python)" && [ -n "$PY" ]; then' in chain
    assert chain.endswith("fi")


def test_shell_chain_single_strategy_emits_if_fi(probe):
    chain = probe.build_probe_shell_chain(port=80, path="/", strategies=("curl",))
    assert chain.startswith("if command -v curl")
    # No "elif" with a single strategy.
    assert " elif " not in chain
    assert chain.endswith("fi")


def test_shell_chain_tail_appended_to_each_branch(probe):
    chain = probe.build_probe_shell_chain(
        port=80, path="/", strategies=("curl", "wget"), tail=" >/dev/null"
    )
    # Tail follows the probe body (before the trailing semicolon).
    assert " >/dev/null;" in chain
    # Twice — once per branch.
    assert chain.count(" >/dev/null;") == 2


def test_shell_chain_no_strategies_raises(probe):
    with pytest.raises(ValueError, match="At least one"):
        probe.build_probe_shell_chain(port=80, path="/", strategies=())
