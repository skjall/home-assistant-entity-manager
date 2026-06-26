"""Tests for the MQTT bridge callbacks: device-name cache and transaction correlation."""

import asyncio
import json
import threading

from bridge_mqtt import MqttBridge


class _Msg:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")


def _bridge():
    # constructing does not connect; we drive the callbacks directly
    b = MqttBridge("host", 1883, base_topic="zigbee2mqtt")
    b._connected.set()
    return b


def test_on_devices_populates_name_cache():
    b = _bridge()
    b._on_devices(
        None,
        None,
        _Msg(
            [{"ieee_address": "0xabc", "friendly_name": "Lamp"}, {"ieee_address": "0xdef", "friendly_name": "Sensor"}]
        ),
    )
    names = asyncio.run(b.get_z2m_names())
    assert names == {"0xabc": "Lamp", "0xdef": "Sensor"}


def test_get_z2m_names_returns_copy():
    b = _bridge()
    b._on_devices(None, None, _Msg([{"ieee_address": "0xabc", "friendly_name": "Lamp"}]))
    names = asyncio.run(b.get_z2m_names())
    names["0xabc"] = "Mutated"
    again = asyncio.run(b.get_z2m_names())
    assert again["0xabc"] == "Lamp"


def test_on_message_correlates_transaction():
    b = _bridge()
    ev = threading.Event()
    b._pending["em_1"] = {"event": ev, "result": None}
    b._on_message(None, None, _Msg({"transaction": "em_1", "status": "ok"}))
    assert ev.is_set()
    assert b._pending["em_1"]["result"]["status"] == "ok"


def test_on_message_ignores_unknown_transaction():
    b = _bridge()
    # no pending entry -> must not raise
    b._on_message(None, None, _Msg({"transaction": "nope", "status": "ok"}))


def test_on_message_ignores_non_json():
    b = _bridge()

    class _Raw:
        payload = b"not-json"

    b._on_message(None, None, _Raw())  # must not raise
