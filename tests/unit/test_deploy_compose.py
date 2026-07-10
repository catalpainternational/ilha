"""Unit tests for the extracted deploy-time compose transforms.

These are now pure module functions (no SlimDeployManager, no SSH/deploy state),
so they can be tested directly. The manager delegates to them; test_slim_deploy_manager
still exercises them through the manager's methods.
"""

from ilha.commands.push.deploy_compose import (
    CODE_SUBDIR,
    retarget_paths_to_code_subdir,
    retarget_relative_path,
    strip_host_port_publishes,
    strip_unnecessary_privileges,
)


def test_strip_host_port_publishes_removes_ports():
    data = {"services": {"db": {"ports": ["5432:5432"]}, "web": {"expose": ["8000"]}}}
    strip_host_port_publishes(data)
    assert "ports" not in data["services"]["db"]
    assert data["services"]["web"] == {"expose": ["8000"]}  # untouched


def test_strip_host_port_publishes_handles_no_services():
    data = {}
    strip_host_port_publishes(data)  # must not raise
    assert data == {}


def test_strip_privileges_keeps_privileged_services():
    data = {
        "services": {
            "web": {"cap_add": ["SYS_ADMIN"], "shm_size": "2gb"},
            "rq-worker-1": {"security_opt": ["seccomp:unconfined"]},
            "db": {"cap_add": ["NET_ADMIN"], "ipc": "host"},
        }
    }
    strip_unnecessary_privileges(data)
    # Privileged (Playwright/Chromium) services keep their capabilities.
    assert data["services"]["web"]["cap_add"] == ["SYS_ADMIN"]
    assert "security_opt" in data["services"]["rq-worker-1"]
    # db is not privileged → stripped.
    assert "cap_add" not in data["services"]["db"]
    assert "ipc" not in data["services"]["db"]


def test_retarget_relative_path_rules():
    assert retarget_relative_path("/abs") == "/abs"
    assert retarget_relative_path("~/x") == "~/x"
    assert retarget_relative_path("$VAR/x") == "$VAR/x"
    assert retarget_relative_path(".") == CODE_SUBDIR
    assert retarget_relative_path("./x") == "./code/x"
    assert retarget_relative_path("../x") == "./code/../x"
    assert retarget_relative_path("named") == "./code/named"
    assert retarget_relative_path("") == ""


def test_retarget_paths_rewrites_build_volumes_env_file():
    data = {
        "services": {
            "web": {
                "build": {"context": "."},
                "volumes": ["./frontend:/app", "named_vol:/data"],
                "env_file": ["./.env", "/abs/.env"],
            }
        }
    }
    retarget_paths_to_code_subdir(data)
    web = data["services"]["web"]
    assert web["build"]["context"] == "./code"
    assert web["volumes"] == ["./code/frontend:/app", "named_vol:/data"]  # named vol untouched
    assert web["env_file"] == ["./code/.env", "/abs/.env"]  # abs untouched
