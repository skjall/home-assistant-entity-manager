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


def test_a_single_network_lets_in_what_it_covers_and_nothing_else():
    """The point of naming a network: the rest of the house stays out."""
    assert external_access.allows("10.2.10.40", "10.2.10.0/24") is True
    assert external_access.allows("10.2.11.40", "10.2.10.0/24") is False
    assert external_access.allows(LAN_PEER, "10.2.10.0/24") is False


def test_a_bare_address_is_that_address_alone():
    assert external_access.allows("10.2.10.40", "10.2.10.40") is True
    assert external_access.allows("10.2.10.41", "10.2.10.40") is False


def test_several_entries_are_added_up():
    configured = "10.2.10.0/24, 192.168.1.40"

    assert external_access.allows("10.2.10.7", configured) is True
    assert external_access.allows("192.168.1.40", configured) is True
    assert external_access.allows("192.168.1.41", configured) is False


def test_a_shorthand_can_stand_among_networks():
    configured = "lan, 203.0.113.7"

    assert external_access.allows(LAN_PEER, configured) is True
    assert external_access.allows("203.0.113.7", configured) is True
    assert external_access.allows("203.0.113.8", configured) is False


def test_an_entry_that_is_not_a_network_is_dropped_without_widening():
    """A typo must neither let everyone in nor throw the good entries away."""
    configured = "10.2.10.0/24, yes please"

    assert external_access.allows("10.2.10.7", configured) is True
    assert external_access.allows(INTERNET_PEER, configured) is False
    assert external_access.describe(configured) == ("10.2.10.0/24",)


def test_nothing_configured_refuses_everyone(monkeypatch):
    monkeypatch.setenv("EXTERNAL_ACCESS", "")

    assert external_access.allows(LAN_PEER) is False
    assert external_access.state() == external_access.OFF


def test_the_state_says_in_one_word_what_is_configured():
    assert external_access.state("") == external_access.OFF
    assert external_access.state("any") == external_access.ANY
    assert external_access.state("10.2.10.0/24") == "some"
    assert external_access.state("lan") == "some"


def test_addresses_the_add_on_cannot_read_count_as_external():
    assert external_access.allows("", external_access.LAN) is False
    assert external_access.allows("not-an-address", external_access.LAN) is False


def test_the_api_mode_falls_back_to_reading_only(monkeypatch):
    monkeypatch.delenv("EXTERNAL_API", raising=False)
    assert external_access.api_mode() == external_access.API_READ

    monkeypatch.setenv("EXTERNAL_API", "nonsense")
    assert external_access.api_mode() == external_access.API_READ

    monkeypatch.setenv("EXTERNAL_API", "write")
    assert external_access.api_mode() == external_access.API_WRITE


def test_an_ipv4_address_mapped_into_ipv6_is_still_local():
    assert external_access.is_local("::ffff:192.168.1.40") is True
    assert external_access.is_local("::ffff:203.0.113.7") is False
