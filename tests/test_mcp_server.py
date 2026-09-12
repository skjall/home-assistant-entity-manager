"""What the MCP server offers, and who is allowed to reach it.

The tools are not written by hand any more: they come from the API description
in api_spec.py. So what is worth checking here is that the description really
becomes working tools, that a reading mode offers nothing that writes, and that
a call reaches the route and comes back with the route's own answer.
"""

import asyncio

import pytest

import api_spec
from api_token_store import ApiTokenStore
import external_access
import mcp_server
import web_ui

INGRESS = "172.30.32.2"
LAN = "192.168.1.50"
INTERNET = "203.0.113.7"


def server_for(chosen):
    return mcp_server.build(web_ui.app, chosen)


def tools_of(server):
    async def ask():
        from fastmcp import Client

        async with Client(server) as client:
            return await client.list_tools()

    return asyncio.run(ask())


def call_tool(name, arguments=None):
    """Call one tool the way a client would, through the whole chain."""

    async def ask():
        from fastmcp import Client

        async with Client(server_for(mcp_server.WRITE)) as client:
            return (await client.call_tool(name, arguments or {})).data

    return asyncio.run(ask())


# --------------------------------------------------------------------------- #
# The setting decides what exists
# --------------------------------------------------------------------------- #


def test_the_setting_decides_whether_there_is_a_server(monkeypatch):
    monkeypatch.delenv("MCP", raising=False)
    assert mcp_server.build(web_ui.app) is None

    monkeypatch.setenv("MCP", "nonsense")
    assert mcp_server.build(web_ui.app) is None


def test_reading_mode_offers_no_tool_that_writes():
    names = {tool.name for tool in tools_of(server_for(mcp_server.READ))}

    assert "rules" in names
    for writing in ("create_rule", "delete_rule", "rename_entity", "apply_naming", "set_exception"):
        assert writing not in names


def test_writing_mode_offers_everything_the_api_has():
    names = {tool.name for tool in tools_of(server_for(mcp_server.WRITE))}

    assert names == {operation.name for operation in api_spec.OPERATIONS}


def test_every_tool_says_what_it_does():
    """A tool an assistant cannot understand is a tool it will misuse."""
    for tool in tools_of(server_for(mcp_server.WRITE)):
        assert tool.description and len(tool.description) > 30, tool.name


# --------------------------------------------------------------------------- #
# The guard in front of the mounted server
# --------------------------------------------------------------------------- #


class _Reached(Exception):
    """Raised by the stand-in app so a test can see the guard let a call past."""


@pytest.fixture
def guarded(tmp_path):
    store = ApiTokenStore(str(tmp_path / "api_token.json"))

    async def app(scope, receive, send):
        raise _Reached()

    import access

    return access.Guard(app, lambda: store), store


def call(guard, peer, token=None):
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    scope = {"type": "http", "path": "/", "client": (peer, 5000), "headers": headers}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    try:
        asyncio.run(guard(scope, receive, send))
    except _Reached:
        return "reached"
    return sent[0]["status"]


def test_ingress_reaches_the_server(guarded, monkeypatch):
    guard, _ = guarded
    monkeypatch.delenv("EXTERNAL_ACCESS", raising=False)

    assert call(guard, INGRESS) == "reached"


def test_without_the_setting_nobody_else_does(guarded, monkeypatch):
    guard, store = guarded
    monkeypatch.delenv("EXTERNAL_ACCESS", raising=False)
    token = store.generate()

    assert call(guard, LAN, token) == 403


def test_a_caller_the_setting_allows_still_needs_a_token(guarded, monkeypatch):
    guard, store = guarded
    monkeypatch.setenv("EXTERNAL_ACCESS", external_access.LAN)
    token = store.generate()

    assert call(guard, LAN) == 401
    assert call(guard, LAN, "em_wrong") == 401
    assert call(guard, LAN, token) == "reached"
    assert call(guard, INTERNET, token) == 403


# --------------------------------------------------------------------------- #
# The tools themselves, run against a small home
# --------------------------------------------------------------------------- #


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A restructurer with two entities on one device, wired into the state."""
    from app_state import renamer_state
    from entity_restructurer import EntityRestructurer
    from naming_overrides import NamingOverrides
    from naming_rules import NamingRules
    from naming_templates import NamingTemplates
    from type_mappings import DEFAULT_SYSTEM_MAPPINGS, TypeMappings

    rules = NamingRules(
        str(tmp_path / "naming_rules.json"),
        device_class_keys=DEFAULT_SYSTEM_MAPPINGS["device_class"].keys(),
        default_language="de",
    )
    overrides = NamingOverrides(str(tmp_path / "naming_overrides.json"))
    restructurer = EntityRestructurer(
        client=object(),
        naming_overrides=overrides,
        type_mappings=TypeMappings(user_mappings_path=str(tmp_path / "unused.json"), rules=rules),
        naming_templates=NamingTemplates(str(tmp_path / "naming_templates.json")),
    )
    restructurer.floors = {"f": {"floor_id": "f", "name": "Erdgeschoss"}}
    restructurer.areas = {"k": {"area_id": "k", "name": "Küche", "floor_id": "f"}}
    restructurer.devices = {"d": {"id": "d", "name": "Deckenleuchte", "area_id": "k"}}
    restructurer.entities = {
        "sensor.a_temperature": {
            "id": "reg-a",
            "entity_id": "sensor.a_temperature",
            "device_id": "d",
            "platform": "mqtt",
            "original_name": "Temperature",
            "translation_key": "temperature",
            "has_entity_name": True,
        },
        "sensor.b_temperature": {
            "id": "reg-b",
            "entity_id": "sensor.b_temperature",
            "device_id": "d",
            "platform": "mqtt",
            "original_name": "Temperature",
            "translation_key": "temperature",
            "has_entity_name": True,
        },
    }

    monkeypatch.setitem(renamer_state, "naming_rules", rules)
    monkeypatch.setitem(renamer_state, "naming_overrides", overrides)
    monkeypatch.setitem(renamer_state, "naming_templates", restructurer.naming_templates)
    monkeypatch.setitem(renamer_state, "type_mappings", restructurer.type_mappings)
    monkeypatch.setitem(renamer_state, "restructurer", restructurer)

    async def already_loaded():
        return None

    # Setting an exception writes the resulting name into Home Assistant; these
    # stand in for it so the test stays about naming rather than about a socket.
    class FakeClient:
        async def get_states(self):
            return []

    class FakeSocket:
        async def connect(self):
            return None

        async def disconnect(self):
            return None

    class FakeRegistry:
        def __init__(self, ws):
            self.ws = ws

        async def update_entity(self, **written):
            restructurer.written = written

    async def fake_client():
        return FakeClient()

    monkeypatch.setattr("routes_entities.init_client", fake_client)
    monkeypatch.setattr("routes_entities.HomeAssistantWebSocket", lambda *a, **k: FakeSocket())
    monkeypatch.setattr("routes_entities.EntityRegistry", FakeRegistry)
    monkeypatch.setenv("HA_URL", "http://supervisor/core")

    monkeypatch.setattr("registry.ensure_registry_loaded", already_loaded)
    monkeypatch.setattr("naming_service.ensure_registry_loaded", already_loaded)
    monkeypatch.setattr("routes_entities.ensure_registry_loaded", already_loaded)
    monkeypatch.setattr("routes_naming.ensure_registry_loaded", already_loaded)
    return restructurer


def test_a_reading_tool_returns_the_route_s_own_answer(home):
    """The whole chain: tool, in-process call, route, answer back."""
    assert call_tool("naming_settings")["language"] == "de"
    assert call_tool("rules")["rules"] == []


def test_a_rule_written_through_a_tool_changes_the_name(home):
    before = call_tool("naming_for", {"entity_id": "sensor.a_temperature"})["rendered"]["entity_name"]

    call_tool(
        "create_rule",
        {"match": {"kind": "translation_key", "value": "temperature"}, "targets": {"de": "Raumtemperatur"}},
    )

    after = call_tool("naming_for", {"entity_id": "sensor.a_temperature"})["rendered"]["entity_name"]
    assert after != before
    assert "Raumtemperatur" in after


def test_an_exception_beats_the_rule_for_one_entity(home):
    call_tool(
        "create_rule",
        {"match": {"kind": "translation_key", "value": "temperature"}, "targets": {"de": "Raumtemperatur"}},
    )
    call_tool("set_exception", {"registry_id": "reg-b", "override_name": "Fühler hinten"})

    first = call_tool("naming_for", {"entity_id": "sensor.a_temperature"})["rendered"]["entity_name"]
    second = call_tool("naming_for", {"entity_id": "sensor.b_temperature"})["rendered"]["entity_name"]

    assert "Raumtemperatur" in first
    assert "Fühler hinten" in second


def test_applying_writes_exactly_what_was_proposed(home, monkeypatch):
    """The tool must hand on the proposal, not a name invented on the way."""
    proposed = call_tool("naming_for", {"entity_id": "sensor.a_temperature"})["rendered"]
    written = {}

    async def record(old_entity_id, new_entity_id=None, friendly_name=None, provenance=None):
        written.update(old=old_entity_id, new=new_entity_id, name=friendly_name)
        return {"success": True}

    monkeypatch.setattr("naming_service.rename_entity", record)

    answer = call_tool("apply_naming", {"entity_ids": ["sensor.a_temperature"]})

    assert answer["renamed"] == 1
    assert written == {
        "old": "sensor.a_temperature",
        "new": proposed["entity_id"],
        "name": proposed["entity_name"],
    }


def test_one_bad_entity_does_not_stop_the_others(home, monkeypatch):
    """An assistant that got one id wrong still gets the rest of its work done."""
    written = []

    async def record(old_entity_id, new_entity_id=None, friendly_name=None, provenance=None):
        written.append(old_entity_id)
        return {"success": True}

    monkeypatch.setattr("naming_service.rename_entity", record)

    answer = call_tool("apply_naming", {"entity_ids": ["sensor.does_not_exist", "sensor.b_temperature"]})

    assert written == ["sensor.b_temperature"]
    assert answer["renamed"] == 1
    assert answer["failed"] == 1
