import pytest

from ilha.commands.push.remote_compose_probe import (
    assert_safe_compose_service_name,
    remote_compose_service_known,
    remote_compose_service_logs_tail,
)


def test_remote_compose_service_known_true_when_ps_returns_id():
    def fake(_u, _s, cmd, _timeout):
        if "ps -q" in cmd:
            return 0, "deadbeef\n", ""
        return 1, "", "should not need config"

    assert remote_compose_service_known(
        fake,
        username="root",
        server="10.0.0.1",
        remote_app_root="/app",
        service_name="web",
    )


def test_remote_compose_service_known_true_when_config_lists_service():
    def fake(_u, _s, cmd, _timeout):
        if "ps -q" in cmd:
            return 0, "\n", ""
        if "config --services" in cmd:
            return 0, "db\nweb\nredis\n", ""
        return 1, "", ""

    assert remote_compose_service_known(
        fake,
        username="root",
        server="10.0.0.1",
        remote_app_root="/app",
        service_name="web",
    )


def test_remote_compose_service_known_false_when_both_fail():
    def fake(_u, _s, cmd, _timeout):
        if "ps -q" in cmd:
            return 0, "", ""
        if "config --services" in cmd:
            return 0, "db\nredis\n", ""
        return 1, "", ""

    assert not remote_compose_service_known(
        fake,
        username="root",
        server="10.0.0.1",
        remote_app_root="/app",
        service_name="web",
    )


def test_remote_compose_service_known_rq_worker_service_name():
    def fake(_u, _s, cmd, _timeout):
        if "ps -q" in cmd and "rq-worker-1" in cmd:
            return 0, "abc\n", ""
        return 1, "", ""

    assert remote_compose_service_known(
        fake,
        username="root",
        server="10.0.0.1",
        remote_app_root="/app",
        service_name="rq-worker-1",
    )


def test_assert_safe_compose_service_name_rejects_injection():
    with pytest.raises(ValueError):
        assert_safe_compose_service_name("web; rm -rf /")


def test_remote_compose_service_logs_tail_clamps_line_count():
    seen = {}

    def fake(_u, _s, cmd, _timeout):
        seen["cmd"] = cmd
        return 0, "ok\n", ""

    remote_compose_service_logs_tail(
        fake,
        username="root",
        server="1.2.0.1",
        remote_app_root="/app",
        service_name="web",
        lines=9999,
    )
    assert "--tail 300" in seen["cmd"]


def test_remote_compose_service_known_invokes_ps_then_config():
    recorded = []

    def fake(_u, _s, cmd, _timeout):
        recorded.append(cmd)
        if "ps -q" in cmd:
            return 0, "", ""
        if "config --services" in cmd:
            return 0, "web\n", ""
        return 1, "", ""

    assert remote_compose_service_known(
        fake,
        username="root",
        server="10.0.0.1",
        remote_app_root="/app",
        service_name="web",
    )
    assert len(recorded) == 2
    assert "ps -q" in recorded[0] and "config --services" in recorded[1]
