import socket

from ilha.scripts.caddy_admin_utils import default_caddy_admin_url, resolve_caddy_admin_url


class _FakeContainer:
    attrs = {
        "NetworkSettings": {
            "Networks": {
                "ilha_caddy_proxy": {
                    "IPAddress": "172.18.0.5",
                }
            }
        }
    }


class _FakeContainers:
    def get(self, _name):
        return _FakeContainer()


class _FakeDockerClient:
    containers = _FakeContainers()


def test_default_caddy_admin_url_prefers_caddy_service_in_container():
    assert default_caddy_admin_url(in_container=True) == "http://caddy:2019"


def test_resolve_caddy_admin_url_falls_back_to_container_ip(monkeypatch):
    monkeypatch.setattr(socket, "gethostbyname", lambda _host: (_ for _ in ()).throw(socket.gaierror()))

    resolved = resolve_caddy_admin_url(
        "http://ilha_caddy_proxy:2019",
        docker_client=_FakeDockerClient(),
    )

    assert resolved == "http://172.18.0.5:2019"


def test_resolve_caddy_admin_url_keeps_localhost():
    assert resolve_caddy_admin_url("http://localhost:2019") == "http://localhost:2019"
