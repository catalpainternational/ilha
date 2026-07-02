"""Comprehensive, deterministic unit tests for ``ilha/utils/validation.py``.

Every subprocess / docker / git interaction is monkeypatched — no real Docker
daemon or git repository is touched. The focus is on:

* ``validate_branch_merged`` being NON-DESTRUCTIVE (never runs ``git branch -d``)
  and using ``git merge-base --is-ancestor <branch> HEAD``.
* Docker inspection helpers using an ANCHORED name filter (``name=^/<name>$``).
* The shared ``_check_docker_prerequisites`` being reached by both
  ``check_prerequisites`` and ``check_prerequisites_no_git``.
* Validation helpers swallowing exceptions and returning ``False``.

BUGS FOUND: none.
"""

import subprocess
from pathlib import Path

import pytest

from ilha.utils import validation
from ilha.utils.validation import (
    DEFAULT_DOCKER_HEALTH_TIMEOUT_SECONDS,
    check_docker_compose_responsive,
    check_docker_daemon_responsive,
    check_prerequisites,
    check_prerequisites_no_git,
    validate_branch_exists,
    validate_branch_merged,
    validate_branch_protection,
    validate_container_exists,
    validate_container_running,
    validate_network_exists,
    validate_volume_exists,
    _check_docker_prerequisites,
)


class FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_recorder(results, *, calls):
    """Return a fake ``subprocess.run`` that records calls and returns/raises.

    ``results`` may be a single FakeCompleted/exception, a list (consumed per
    call), or a callable ``(cmd, kwargs) -> FakeCompleted``.
    """

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), kwargs))
        outcome = results
        if isinstance(results, list):
            outcome = results[len(calls) - 1]
        if callable(outcome):
            outcome = outcome(cmd, kwargs)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return fake_run


@pytest.fixture
def calls():
    return []


# ---------------------------------------------------------------------------
# validate_branch_merged  — the safety-critical, non-destructive predicate
# ---------------------------------------------------------------------------


def test_validate_branch_merged_uses_is_ancestor(monkeypatch, calls, tmp_path):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=0), calls=calls),
    )
    result = validate_branch_merged("feature-x", project_root=tmp_path)
    assert result is True
    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd == ["git", "merge-base", "--is-ancestor", "feature-x", "HEAD"]
    assert kwargs.get("cwd") == tmp_path
    # It must NOT pass check=True — it inspects returncode instead.
    assert kwargs.get("check") in (None, False)


def test_validate_branch_merged_true_on_rc0(monkeypatch, calls, tmp_path):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=0), calls=calls),
    )
    assert validate_branch_merged("merged-branch", project_root=tmp_path) is True


def test_validate_branch_merged_false_on_rc1(monkeypatch, calls, tmp_path):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=1), calls=calls),
    )
    assert validate_branch_merged("unmerged-branch", project_root=tmp_path) is False


def test_validate_branch_merged_false_on_nonstandard_rc(monkeypatch, calls, tmp_path):
    # Any non-zero return code (e.g. bad revision -> 128) means "not merged".
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=128), calls=calls),
    )
    assert validate_branch_merged("nope", project_root=tmp_path) is False


def test_validate_branch_merged_is_non_destructive(monkeypatch, calls, tmp_path):
    """CRITICAL regression guard: it must NEVER delete a branch.

    Previously this helper ran ``git branch -d`` which *deletes* a merged
    branch as a side effect. Reading merge status must never mutate the repo.
    """
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=0), calls=calls),
    )
    validate_branch_merged("feature-x", project_root=tmp_path)

    for cmd, _kwargs in calls:
        # No branch deletion in any form.
        assert cmd[:2] != ["git", "branch"], f"must not touch branches: {cmd}"
        assert "-d" not in cmd, f"destructive -d flag found: {cmd}"
        assert "-D" not in cmd, f"destructive -D flag found: {cmd}"
        assert "delete" not in cmd, f"delete verb found: {cmd}"


def test_validate_branch_merged_false_on_exception(monkeypatch, calls, tmp_path):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(OSError("git missing"), calls=calls),
    )
    assert validate_branch_merged("feature-x", project_root=tmp_path) is False


def test_validate_branch_merged_resolves_project_root_when_none(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=0), calls=calls),
    )
    sentinel = Path("/some/resolved/root")
    import ilha.config.settings as settings

    monkeypatch.setattr(settings, "get_project_root", lambda: sentinel)
    assert validate_branch_merged("feature-x") is True
    assert calls[0][1]["cwd"] == sentinel


# ---------------------------------------------------------------------------
# validate_container_running — anchored filter, "Up" only
# ---------------------------------------------------------------------------


def test_validate_container_running_uses_anchored_filter(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=0, stdout="Up 3 hours"), calls=calls),
    )
    assert validate_container_running("web") is True
    cmd, kwargs = calls[0]
    assert cmd == [
        "docker",
        "ps",
        "--filter",
        "name=^/web$",
        "--format",
        "{{.Status}}",
    ]
    assert kwargs.get("check") is True


def test_validate_container_running_true_only_on_up(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=0, stdout="Up 5 seconds (healthy)"), calls=calls),
    )
    assert validate_container_running("db") is True


def test_validate_container_running_false_when_exited(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=0, stdout="Exited (0) 2 minutes ago"), calls=calls),
    )
    assert validate_container_running("db") is False


def test_validate_container_running_false_when_empty(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=0, stdout=""), calls=calls),
    )
    assert validate_container_running("db") is False


def test_validate_container_running_false_on_exception(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(subprocess.CalledProcessError(1, "docker"), calls=calls),
    )
    assert validate_container_running("db") is False


def test_validate_container_running_anchor_prevents_substring_match(monkeypatch, calls):
    """The anchored filter is what stops 'web' matching 'web2'. Assert the exact
    filter string so a regression back to a bare substring filter is caught."""
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=0, stdout=""), calls=calls),
    )
    validate_container_running("web")
    filter_arg = calls[0][0][calls[0][0].index("--filter") + 1]
    assert filter_arg.startswith("name=^/")
    assert filter_arg.endswith("$")
    assert filter_arg == "name=^/web$"


# ---------------------------------------------------------------------------
# validate_container_exists — anchored filter, name membership
# ---------------------------------------------------------------------------


def test_validate_container_exists_uses_anchored_filter(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=0, stdout="myproj-web\n"), calls=calls),
    )
    assert validate_container_exists("myproj-web") is True
    cmd, _kwargs = calls[0]
    assert cmd == [
        "docker",
        "ps",
        "-a",
        "--filter",
        "name=^/myproj-web$",
        "--format",
        "{{.Names}}",
    ]


def test_validate_container_exists_false_when_absent(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=0, stdout=""), calls=calls),
    )
    assert validate_container_exists("ghost") is False


def test_validate_container_exists_false_on_exception(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(Exception("boom"), calls=calls),
    )
    assert validate_container_exists("ghost") is False


# ---------------------------------------------------------------------------
# validate_volume_exists / validate_network_exists — docker inspect wrappers
# ---------------------------------------------------------------------------


def test_validate_volume_exists_true(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=0), calls=calls),
    )
    assert validate_volume_exists("myvol") is True
    cmd, kwargs = calls[0]
    assert cmd == ["docker", "volume", "inspect", "myvol"]
    assert kwargs.get("check") is True


def test_validate_volume_exists_false_on_calledprocess(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(subprocess.CalledProcessError(1, "docker"), calls=calls),
    )
    assert validate_volume_exists("missing") is False


def test_validate_volume_exists_false_on_oserror(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(OSError("no docker"), calls=calls),
    )
    assert validate_volume_exists("missing") is False


def test_validate_network_exists_true(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=0), calls=calls),
    )
    assert validate_network_exists("mynet") is True
    cmd, kwargs = calls[0]
    assert cmd == ["docker", "network", "inspect", "mynet"]
    assert kwargs.get("check") is True


def test_validate_network_exists_false_on_error(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(subprocess.CalledProcessError(1, "docker"), calls=calls),
    )
    assert validate_network_exists("missing") is False


# ---------------------------------------------------------------------------
# validate_branch_exists — git show-ref
# ---------------------------------------------------------------------------


def test_validate_branch_exists_true(monkeypatch, calls, tmp_path):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=0), calls=calls),
    )
    assert validate_branch_exists("main", project_root=tmp_path) is True
    cmd, kwargs = calls[0]
    assert cmd == ["git", "show-ref", "--verify", "--quiet", "refs/heads/main"]
    assert kwargs.get("cwd") == tmp_path
    assert kwargs.get("check") is True


def test_validate_branch_exists_false_on_calledprocess(monkeypatch, calls, tmp_path):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(subprocess.CalledProcessError(1, "git"), calls=calls),
    )
    assert validate_branch_exists("nope", project_root=tmp_path) is False


def test_validate_branch_exists_false_on_exception(monkeypatch, calls, tmp_path):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(RuntimeError("kaboom"), calls=calls),
    )
    assert validate_branch_exists("nope", project_root=tmp_path) is False


# ---------------------------------------------------------------------------
# validate_branch_protection — pure predicate against PROTECTED_BRANCHES
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["main", "master"])
def test_validate_branch_protection_protected(name):
    assert validate_branch_protection(name) is True


@pytest.mark.parametrize("name", ["develop", "staging", "production", "feature-x", ""])
def test_validate_branch_protection_unprotected(name):
    assert validate_branch_protection(name) is False


# ---------------------------------------------------------------------------
# check_docker_daemon_responsive — ok / timeout / nonzero / missing binary
# ---------------------------------------------------------------------------


def test_daemon_responsive_ok(monkeypatch, calls):
    monkeypatch.setattr(validation.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=0), calls=calls),
    )
    ok, reason = check_docker_daemon_responsive()
    assert ok is True
    assert reason is None
    cmd, kwargs = calls[0]
    assert cmd == ["docker", "info"]
    assert kwargs.get("timeout") == DEFAULT_DOCKER_HEALTH_TIMEOUT_SECONDS
    assert kwargs.get("check") is False


def test_daemon_responsive_missing_binary(monkeypatch, calls):
    monkeypatch.setattr(validation.shutil, "which", lambda _n: None)
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=0), calls=calls),
    )
    ok, reason = check_docker_daemon_responsive()
    assert ok is False
    assert "not found on PATH" in reason
    # Must short-circuit before invoking docker.
    assert calls == []


def test_daemon_responsive_timeout(monkeypatch, calls):
    monkeypatch.setattr(validation.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(subprocess.TimeoutExpired(cmd="docker info", timeout=20), calls=calls),
    )
    ok, reason = check_docker_daemon_responsive(timeout_seconds=20)
    assert ok is False
    assert "timed out after 20s" in reason


def test_daemon_responsive_oserror(monkeypatch, calls):
    monkeypatch.setattr(validation.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(OSError("permission denied"), calls=calls),
    )
    ok, reason = check_docker_daemon_responsive()
    assert ok is False
    assert "Could not run `docker info`" in reason
    assert "permission denied" in reason


def test_daemon_responsive_nonzero_reports_stderr_first_line(monkeypatch, calls):
    monkeypatch.setattr(validation.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(
            FakeCompleted(returncode=1, stderr="Cannot connect to the Docker daemon\nsecond line"),
            calls=calls,
        ),
    )
    ok, reason = check_docker_daemon_responsive()
    assert ok is False
    assert "exit 1" in reason
    assert "Cannot connect to the Docker daemon" in reason
    assert "second line" not in reason


def test_daemon_responsive_nonzero_truncates_long_output(monkeypatch, calls):
    monkeypatch.setattr(validation.shutil, "which", lambda _n: "/usr/bin/docker")
    long_msg = "x" * 500
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=1, stderr=long_msg), calls=calls),
    )
    ok, reason = check_docker_daemon_responsive()
    assert ok is False
    assert reason.endswith("...")


def test_daemon_responsive_nonzero_no_output(monkeypatch, calls):
    monkeypatch.setattr(validation.shutil, "which", lambda _n: "/usr/bin/docker")
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=1, stdout="", stderr=""), calls=calls),
    )
    ok, reason = check_docker_daemon_responsive()
    assert ok is False
    assert "no output from docker" in reason


# ---------------------------------------------------------------------------
# check_docker_compose_responsive — v2 ok / fallback / timeout / both fail
# ---------------------------------------------------------------------------


def test_compose_responsive_v2_ok(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(FakeCompleted(returncode=0, stdout="Docker Compose v2"), calls=calls),
    )
    ok, reason = check_docker_compose_responsive()
    assert ok is True
    assert reason is None
    assert calls[0][0] == ["docker", "compose", "version"]
    assert len(calls) == 1  # no fallback needed


def test_compose_responsive_falls_back_to_legacy(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(
            [
                FakeCompleted(returncode=1, stderr="no v2"),
                FakeCompleted(returncode=0, stdout="docker-compose 1.29"),
            ],
            calls=calls,
        ),
    )
    ok, reason = check_docker_compose_responsive()
    assert ok is True
    assert reason is None
    assert calls[0][0] == ["docker", "compose", "version"]
    assert calls[1][0] == ["docker-compose", "version"]


def test_compose_responsive_v2_timeout_short_circuits(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(
            subprocess.TimeoutExpired(cmd="docker compose version", timeout=20), calls=calls
        ),
    )
    ok, reason = check_docker_compose_responsive(timeout_seconds=20)
    assert ok is False
    assert "timed out after 20s" in reason
    # Timeout on v2 returns immediately; no legacy attempt.
    assert len(calls) == 1


def test_compose_responsive_both_fail(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(
            [
                FakeCompleted(returncode=1, stderr="v2 broken"),
                FakeCompleted(returncode=1, stderr="legacy broken"),
            ],
            calls=calls,
        ),
    )
    ok, reason = check_docker_compose_responsive()
    assert ok is False
    assert "Neither" in reason
    assert "v2 broken" in reason
    assert len(calls) == 2


def test_compose_responsive_legacy_timeout(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(
            [
                FakeCompleted(returncode=1, stderr="v2 broken"),
                subprocess.TimeoutExpired(cmd="docker-compose version", timeout=20),
            ],
            calls=calls,
        ),
    )
    ok, reason = check_docker_compose_responsive(timeout_seconds=20)
    assert ok is False
    assert "Neither `docker compose` nor `docker-compose` responded" in reason


def test_compose_responsive_v2_oserror_then_legacy_ok(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(
            [
                OSError("v2 not installed"),
                FakeCompleted(returncode=0, stdout="docker-compose 1.29"),
            ],
            calls=calls,
        ),
    )
    ok, reason = check_docker_compose_responsive()
    assert ok is True
    assert reason is None


def test_compose_responsive_legacy_oserror_falls_through_to_failure(monkeypatch, calls):
    monkeypatch.setattr(
        validation.subprocess,
        "run",
        make_recorder(
            [
                FakeCompleted(returncode=1, stderr="v2 broken"),
                OSError("legacy not installed"),
            ],
            calls=calls,
        ),
    )
    ok, reason = check_docker_compose_responsive()
    assert ok is False
    assert "Neither" in reason


# ---------------------------------------------------------------------------
# _check_docker_prerequisites — shared helper reached by both entrypoints
# ---------------------------------------------------------------------------


def test_check_docker_prereq_passes_when_both_ok(monkeypatch):
    monkeypatch.setattr(validation, "check_docker_daemon_responsive", lambda **_k: (True, None))
    monkeypatch.setattr(validation, "check_docker_compose_responsive", lambda **_k: (True, None))
    # Should not raise.
    _check_docker_prerequisites()


def test_check_docker_prereq_exits_on_daemon_failure(monkeypatch):
    monkeypatch.setattr(
        validation, "check_docker_daemon_responsive", lambda **_k: (False, "daemon down")
    )
    # Compose check should never be reached; make it explode if it is.
    monkeypatch.setattr(
        validation,
        "check_docker_compose_responsive",
        lambda **_k: (_ for _ in ()).throw(AssertionError("compose checked despite daemon fail")),
    )
    with pytest.raises(SystemExit) as exc:
        _check_docker_prerequisites()
    assert exc.value.code == 1


def test_check_docker_prereq_exits_on_compose_failure(monkeypatch):
    monkeypatch.setattr(validation, "check_docker_daemon_responsive", lambda **_k: (True, None))
    monkeypatch.setattr(
        validation, "check_docker_compose_responsive", lambda **_k: (False, "no compose")
    )
    with pytest.raises(SystemExit) as exc:
        _check_docker_prerequisites()
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# check_prerequisites — git + shared docker checks
# ---------------------------------------------------------------------------


def test_check_prerequisites_invokes_shared_docker_checks(monkeypatch):
    monkeypatch.setattr(validation, "validate_git_repository", lambda _r=None: True)
    called = {"n": 0}

    def fake_shared():
        called["n"] += 1

    monkeypatch.setattr(validation, "_check_docker_prerequisites", fake_shared)
    check_prerequisites()
    assert called["n"] == 1


def test_check_prerequisites_exits_when_not_git_repo(monkeypatch):
    monkeypatch.setattr(validation, "validate_git_repository", lambda _r=None: False)
    # Docker check must not run if git fails first.
    monkeypatch.setattr(
        validation,
        "_check_docker_prerequisites",
        lambda: (_ for _ in ()).throw(AssertionError("docker checked despite git fail")),
    )
    with pytest.raises(SystemExit) as exc:
        check_prerequisites()
    assert exc.value.code == 1


def test_check_prerequisites_docker_daemon_failure_exits(monkeypatch):
    monkeypatch.setattr(validation, "validate_git_repository", lambda _r=None: True)
    monkeypatch.setattr(
        validation, "check_docker_daemon_responsive", lambda **_k: (False, "daemon down")
    )
    with pytest.raises(SystemExit):
        check_prerequisites()


# ---------------------------------------------------------------------------
# check_prerequisites_no_git — shared docker checks only, no git
# ---------------------------------------------------------------------------


def test_check_prerequisites_no_git_invokes_shared_docker_checks(monkeypatch):
    called = {"n": 0}

    def fake_shared():
        called["n"] += 1

    monkeypatch.setattr(validation, "_check_docker_prerequisites", fake_shared)
    # git validator must NOT be consulted.
    monkeypatch.setattr(
        validation,
        "validate_git_repository",
        lambda _r=None: (_ for _ in ()).throw(AssertionError("git validated in no_git path")),
    )
    check_prerequisites_no_git()
    assert called["n"] == 1


def test_check_prerequisites_no_git_exits_on_docker_failure(monkeypatch):
    monkeypatch.setattr(
        validation, "check_docker_daemon_responsive", lambda **_k: (False, "daemon down")
    )
    with pytest.raises(SystemExit) as exc:
        check_prerequisites_no_git()
    assert exc.value.code == 1


def test_check_prerequisites_no_git_passes_when_docker_ok(monkeypatch):
    monkeypatch.setattr(validation, "check_docker_daemon_responsive", lambda **_k: (True, None))
    monkeypatch.setattr(validation, "check_docker_compose_responsive", lambda **_k: (True, None))
    # Should not raise.
    check_prerequisites_no_git()
