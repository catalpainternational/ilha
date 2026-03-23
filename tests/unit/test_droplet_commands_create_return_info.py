from types import SimpleNamespace

from ilha.commands.droplets import DropletCommands


def test_create_droplet_can_return_droplet(monkeypatch):
    cmd = DropletCommands()
    fake_droplet = SimpleNamespace(
        id=123,
        name="demo",
        ip_address="1.2.3.4",
        status="active",
        region="nyc1",
        size="s-1vcpu-1gb",
        image="ubuntu-22-04-x64",
        tags=[],
        private_ip_address=None,
        vpc_uuid=None,
    )

    class _Provider:
        def create_droplet(self, **_kwargs):
            return fake_droplet

        def wait_for_droplet_ready(self, _droplet_id):
            return True

        def get_droplet(self, _droplet_id):
            return fake_droplet

    monkeypatch.setattr("ilha.commands.droplets.DropletManager.resolve_droplet_token", lambda _x: "token")
    monkeypatch.setattr("ilha.commands.droplets.DropletManager.get_droplet_defaults", lambda: {})
    monkeypatch.setattr("ilha.commands.droplets.DropletManager.create_provider", lambda *_args, **_kwargs: _Provider())

    result = cmd.create_droplet(
        name="demo",
        wait=True,
        api_token="token",
        return_droplet=True,
    )
    assert result is fake_droplet
