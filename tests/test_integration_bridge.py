"""Tests for the integration bridge: integration/IEEE extraction and adapter selection."""

import asyncio

from bridge_adapters import build_bridge
from bridge_mqtt_adapter import MqttZ2MAdapter
from integration_bridge import (
    extract_integrations,
    extract_z2m_ieee,
)


def test_extract_integrations():
    dev = {"identifiers": [["mqtt", "zigbee2mqtt_0xabc"], ["matter", "serial_x"]]}
    assert set(extract_integrations(dev)) == {"mqtt", "matter"}


def test_extract_integrations_strips_colon_domain():
    dev = {"identifiers": [["homekit_controller:accessory-id", "x"]]}
    assert extract_integrations(dev) == ["homekit_controller"]


def test_extract_z2m_ieee():
    assert extract_z2m_ieee({"identifiers": [["mqtt", "zigbee2mqtt_0x001788010cd81c13"]]}) == "0x001788010cd81c13"


def test_extract_z2m_ieee_ignores_non_z2m_and_bridge():
    assert extract_z2m_ieee({"identifiers": [["mqtt", "[301DEEE4]"]]}) is None
    assert extract_z2m_ieee({"identifiers": [["mqtt", "zigbee2mqtt_bridge_0x84b4"]]}) is None
    assert extract_z2m_ieee({"identifiers": [["matter", "serial_x"]]}) is None


class _DR:
    """Antwortet wie Home Assistant: das Gerät, wie es nach dem Entfernen
    aussieht - und nichts, wenn es weg ist.

    ``survives`` sind die Config-Entries, nach deren Entfernen das Gerät noch
    steht; das ist der Fall, der ein Matter-Altgerät zurückbleiben ließ.
    """

    def __init__(self, survives=(), remaining=()):
        self.calls = []
        self.survives = set(survives)
        self.remaining = list(remaining)

    async def remove_config_entry(self, device_id, config_entry_id):
        self.calls.append((device_id, config_entry_id))
        if config_entry_id in self.survives:
            return {"success": True, "result": {"id": device_id, "config_entries": list(self.remaining)}}
        return {"success": True}


def test_build_bridge_without_mqtt_has_no_z2m_adapter():
    bridge = build_bridge(_DR(), mqtt_bridge=None)
    assert not any(isinstance(a, MqttZ2MAdapter) for a in bridge._adapters)


def test_adapter_selection():
    bridge = build_bridge(_DR(), mqtt_bridge=object())
    z2m = {"identifiers": [["mqtt", "zigbee2mqtt_0x00158d0001abcdef"]]}
    other_mqtt = {"identifiers": [["mqtt", "[301DEEE4]"]]}
    matter = {"identifiers": [["matter", "serial_x"]]}
    assert type(bridge.select_adapter(z2m)).__name__ == "MqttZ2MAdapter"
    assert type(bridge.select_adapter(other_mqtt)).__name__ == "RegistryAdapter"
    assert type(bridge.select_adapter(matter)).__name__ == "MatterAdapter"


def test_registry_remove_native_uses_device_id_key():
    dr = _DR()
    bridge = build_bridge(dr, mqtt_bridge=None)
    snap = {"device_id": "fe443", "integrations": ["matter"], "config_entries": ["01ABC"]}
    res = asyncio.run(bridge.remove_native(snap, force=True))
    assert res.success
    assert dr.calls == [("fe443", "01ABC")]


def test_registry_remove_native_missing_device_id():
    dr = _DR()
    bridge = build_bridge(dr, mqtt_bridge=None)
    res = asyncio.run(bridge.remove_native({"config_entries": ["01ABC"]}, force=True))
    assert not res.success
    assert dr.calls == []


MATTER = {"device_id": "fe443", "integrations": ["matter"], "config_entries": ["01ABC", "02DEF"]}


def test_a_device_held_by_several_entries_loses_all_of_them():
    """Ein Gerät verschwindet erst, wenn der letzte Config-Entry weg ist. Nur
    den ersten zu entfernen ließ das Altgerät stehen."""
    dr = _DR(survives=["01ABC"], remaining=["02DEF"])
    bridge = build_bridge(dr, mqtt_bridge=None)

    res = asyncio.run(bridge.remove_native(MATTER, force=True))

    assert res.success
    assert dr.calls == [("fe443", "01ABC"), ("fe443", "02DEF")]


def test_the_answer_decides_which_entry_comes_next():
    """Der Schnappschuss kann alt sein; Home Assistant weiß es genauer."""
    dr = _DR(survives=["01ABC"], remaining=["03GHI"])
    bridge = build_bridge(dr, mqtt_bridge=None)

    res = asyncio.run(bridge.remove_native(MATTER, force=True))

    assert res.success
    assert dr.calls == [("fe443", "01ABC"), ("fe443", "03GHI")]


def test_a_device_that_stays_is_reported_as_a_failure():
    """Erfolg zu melden, während das Altgerät noch in der Liste steht, wäre
    die unangenehmste Variante: der Nutzer erfährt es erst beim Nachsehen."""
    dr = _DR(survives=["01ABC", "02DEF"], remaining=["02DEF"])
    bridge = build_bridge(dr, mqtt_bridge=None)

    res = asyncio.run(bridge.remove_native(MATTER, force=True))

    assert not res.success
    assert "still in the registry" in res.error


def test_one_entry_is_still_the_common_case():
    dr = _DR()
    bridge = build_bridge(dr, mqtt_bridge=None)

    res = asyncio.run(bridge.remove_native({"device_id": "fe443", "config_entries": ["01ABC"]}, force=True))

    assert res.success
    assert dr.calls == [("fe443", "01ABC")]
