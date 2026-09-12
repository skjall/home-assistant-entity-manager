"""Who the add-on answers when a request does not come through Ingress."""

import pytest

from api_token_store import ApiTokenStore
import external_access
import web_ui

INGRESS_PEER = "172.30.32.2"
LAN_PEER = "192.168.1.40"
INTERNET_PEER = "203.0.113.7"


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = ApiTokenStore(str(tmp_path / "api_token.json"))
    monkeypatch.setitem(web_ui.renamer_state, "api_token_store", store)
    monkeypatch.delenv("EXTERNAL_ACCESS", raising=False)
    web_ui.app.config["TESTING"] = True
    return web_ui.app.test_client(), store


def get(client, path, peer, **kwargs):
    """Make a request that looks like it arrived from ``peer``."""
    return client.get(path, environ_overrides={"REMOTE_ADDR": peer}, **kwargs)


def test_the_default_refuses_every_direct_caller():
    assert external_access.allows(LAN_PEER, external_access.OFF) is False
    assert external_access.allows(INTERNET_PEER, external_access.OFF) is False


def test_the_supervisor_network_is_recognised():
    assert external_access.is_supervisor(INGRESS_PEER) is True
    assert external_access.is_supervisor(LAN_PEER) is False
    assert external_access.is_supervisor("") is False


def test_the_local_setting_lets_the_home_network_in_and_nobody_else():
    assert external_access.allows(LAN_PEER, external_access.LAN) is True
    assert external_access.allows(INTERNET_PEER, external_access.LAN) is False


def test_the_open_setting_lets_everyone_in():
    assert external_access.allows(INTERNET_PEER, external_access.ANY) is True


def test_an_unknown_setting_refuses(monkeypatch):
    monkeypatch.setenv("EXTERNAL_ACCESS", "yes please")

    assert external_access.policy() == external_access.OFF


def test_addresses_the_add_on_cannot_read_count_as_external():
    assert external_access.allows("", external_access.LAN) is False
    assert external_access.allows("not-an-address", external_access.LAN) is False


def test_an_ipv4_address_mapped_into_ipv6_is_still_local():
    assert external_access.is_local("::ffff:192.168.1.40") is True
    assert external_access.is_local("::ffff:203.0.113.7") is False
