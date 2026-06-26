"""Tests for the integration bridge: integration/IEEE extraction and adapter selection."""

import asyncio

from bridge_adapters import build_bridge
from bridge_mqtt_adapter import MqttZ2MAdapter
from integration_bridge import (
    extract_config_entry_id,
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


def test_extract_config_entry_id():
    assert extract_config_entry_id({"config_entries": ["01ABC"]}) == "01ABC"
    assert extract_config_entry_id({"config_entries": []}) is None


class _DR:
    def __init__(self):
        self.calls = []

    async def remove_config_entry(self, device_id, config_entry_id):
        self.calls.append((device_id, config_entry_id))
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
