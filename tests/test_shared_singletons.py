"""Objects that exist once must exist once even when threads race for them.

Building the client or the reference checker is a read followed by a write. With
several worker threads both can find nothing there and both build; the loser's
object is dropped, and with it whatever it had already fetched.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

import app_state
import reference_cache
import registry

CALLERS = 8


def from_threads(call):
    """Let every thread arrive at the same moment, then call."""
    ready = threading.Barrier(CALLERS)

    def run(_):
        ready.wait()
        return call()

    with ThreadPoolExecutor(max_workers=CALLERS) as pool:
        return list(pool.map(run, range(CALLERS)))


@pytest.fixture
def counted_checker(monkeypatch):
    built = []

    class SlowChecker:
        def __init__(self, base_url, token):
            built.append(self)
            # Widens the window a real constructor leaves open anyway.
            time.sleep(0.05)

        def invalidate_cache(self):
            pass

    monkeypatch.setattr(reference_cache, "ReferenceChecker", SlowChecker)
    monkeypatch.setattr(reference_cache, "_reference_checker", None)
    return built


def test_the_reference_checker_is_built_once(counted_checker):
    checkers = from_threads(reference_cache.get_reference_checker)

    assert len(counted_checker) == 1
    assert all(checker is checkers[0] for checker in checkers)


def test_invalidating_reaches_the_checker_everyone_shares(counted_checker):
    dropped = []
    checker = reference_cache.get_reference_checker()
    checker.invalidate_cache = lambda: dropped.append(True)

    reference_cache.invalidate_reference_checker_cache()

    assert dropped == [True]


@pytest.fixture
def counted_client(monkeypatch):
    built = []

    class SlowClient:
        def __init__(self, base_url, token):
            built.append(self)
            time.sleep(0.05)

    monkeypatch.setattr(app_state, "HomeAssistantClient", SlowClient)
    monkeypatch.setattr(app_state, "EntityRestructurer", lambda *a, **k: object())
    monkeypatch.setitem(app_state.renamer_state, "client", None)
    monkeypatch.setitem(app_state.renamer_state, "restructurer", None)
    return built


def test_the_home_assistant_client_is_built_once(counted_client):
    clients = from_threads(lambda: asyncio.run(app_state.init_client()))

    assert len(counted_client) == 1
    assert all(client is clients[0] for client in clients)
    assert app_state.renamer_state["restructurer"] is not None


@pytest.fixture
def counted_bridge(monkeypatch):
    attempts = []

    async def connect():
        attempts.append(True)
        await asyncio.sleep(0.05)
        app_state.renamer_state["mqtt_bridge"] = "bridge"
        return "bridge"

    monkeypatch.setattr(app_state, "_connect_mqtt_bridge", connect)
    monkeypatch.setitem(app_state.renamer_state, "mqtt_bridge", None)
    monkeypatch.setitem(app_state.renamer_state, "mqtt_bridge_tried", False)
    monkeypatch.setenv("ENABLE_Z2M_BRIDGE", "true")
    return attempts


def test_the_broker_is_contacted_once(counted_bridge):
    bridges = from_threads(lambda: asyncio.run(app_state.ensure_mqtt_bridge()))

    assert counted_bridge == [True]
    assert bridges == ["bridge"] * CALLERS


@pytest.fixture
def counted_registry(monkeypatch):
    reads = []

    class FakeRestructurer:
        def __init__(self):
            self.entities = {}

        async def load_structure(self, ws):
            reads.append(True)
            await asyncio.sleep(0.05)
            self.entities = {"sensor.a": {}}

    class FakeSocket:
        async def connect(self):
            pass

        async def disconnect(self):
            pass

    async def nothing(ws):
        pass

    monkeypatch.setattr(registry, "HomeAssistantWebSocket", lambda *a, **k: FakeSocket())
    monkeypatch.setattr(registry, "sync_ha_language", nothing)
    monkeypatch.setitem(app_state.renamer_state, "restructurer", FakeRestructurer())
    return reads


def test_the_registry_is_read_once_for_everyone_waiting(counted_registry):
    """Eight callers, one socket: the rest wait and find the entities there."""
    from_threads(lambda: asyncio.run(registry.ensure_registry_loaded()))

    assert counted_registry == [True]
    assert app_state.renamer_state["restructurer"].entities
