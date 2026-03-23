import requests

from ilha.core.dns_providers.digitalocean import DigitalOceanProvider


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(response=self)

    def json(self):
        return self._payload


def test_make_request_retries_on_429(monkeypatch):
    provider = DigitalOceanProvider("token")
    calls = {"count": 0}

    def fake_request(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return _FakeResponse(429, headers={"Retry-After": "1"}, text="rate limit")
        return _FakeResponse(200, payload={"ok": True})

    monkeypatch.setattr(provider.session, "request", fake_request)
    monkeypatch.setattr("ilha.core.dns_providers.digitalocean.time.sleep", lambda _x: None)
    resp = provider._make_request("GET", "/droplets")
    assert resp is not None
    assert calls["count"] == 2


def test_collect_paginated_accumulates_all_pages(monkeypatch):
    provider = DigitalOceanProvider("token")

    def fake_make_request(_method, endpoint, **_kwargs):
        if "page=1" in endpoint:
            return _FakeResponse(
                200,
                payload={
                    "droplets": [{"id": 1}, {"id": 2}],
                    "links": {"pages": {"next": "next"}},
                },
            )
        if "page=2" in endpoint:
            return _FakeResponse(
                200,
                payload={
                    "droplets": [{"id": 3}],
                    "links": {"pages": {}},
                },
            )
        return None

    monkeypatch.setattr(provider, "_make_request", fake_make_request)
    items = provider._collect_paginated("/droplets", "droplets")
    assert [i["id"] for i in items] == [1, 2, 3]


def test_list_droplets_uses_pagination(monkeypatch):
    provider = DigitalOceanProvider("token")

    monkeypatch.setattr(
        provider,
        "_collect_paginated",
        lambda endpoint, key: [
            {
                "id": 100,
                "name": "demo",
                "status": "active",
                "region": {"slug": "nyc1"},
                "size_slug": "s-1vcpu-1gb",
                "image": {"slug": "ubuntu-22-04-x64"},
                "tags": [],
                "networks": {"v4": [{"type": "public", "ip_address": "1.2.3.4"}]},
            }
        ],
    )
    droplets = provider.list_droplets()
    assert len(droplets) == 1
    assert droplets[0].id == 100
    assert droplets[0].ip_address == "1.2.3.4"
