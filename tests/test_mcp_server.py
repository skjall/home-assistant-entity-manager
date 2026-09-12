"""What the MCP server offers, and who is allowed to reach it."""

import asyncio

import pytest

from api_token_store import ApiTokenStore
import external_access
import mcp_server

INGRESS = "172.30.32.2"
LAN = "192.168.1.50"
INTERNET = "203.0.113.7"


def tool_names(server):
    async def ask():
        from fastmcp import Client

        async with Client(server) as client:
            return sorted(tool.name for tool in await client.list_tools())

    return asyncio.run(ask())


def test_the_setting_decides_whether_there_is_a_server(monkeypatch):
    monkeypatch.delenv("MCP", raising=False)
    assert mcp_server.build() is None

    monkeypatch.setenv("MCP", "nonsense")
    assert mcp_server.build() is None


def test_reading_mode_offers_no_tool_that_writes():
    server = mcp_server.build(mcp_server.READ)

    names = tool_names(server)

    assert "naming_for" in names
    assert "set_rule" not in names
    assert "apply_naming" not in names


def test_writing_mode_offers_both():
    server = mcp_server.build(mcp_server.WRITE)

    names = tool_names(server)

    assert "naming_for" in names
    assert {"set_rule", "delete_rule", "set_exception", "apply_naming"} <= set(names)


def test_every_tool_says_what_it_does():
    """A tool an assistant cannot read the purpose of is a tool it misuses."""
    server = mcp_server.build(mcp_server.WRITE)

    async def ask():
        from fastmcp import Client

        async with Client(server) as client:
            return await client.list_tools()

    for tool in asyncio.run(ask()):
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

    monkeypatch.setattr("registry.ensure_registry_loaded", already_loaded)
    monkeypatch.setattr("naming_service.ensure_registry_loaded", already_loaded)
    monkeypatch.setattr("mcp_server.ensure_registry_loaded", already_loaded)
    return restructurer


def run(tool, **arguments):
    """Call one tool the way a client would, through the server."""
    server = mcp_server.build(mcp_server.WRITE)

    async def ask():
        from fastmcp import Client

        async with Client(server) as client:
            return (await client.call_tool(tool, arguments)).data

    return asyncio.run(ask())


def test_every_reading_tool_answers(home):
    """Each one is called for real: a wrong attribute shows up here, not live."""
    assert run("list_areas")[0]["name"] == "Küche"
    assert run("naming_settings")["language"] == "de"
    assert run("list_rules") == []
    found = run("find_entities", query="temperature")
    assert {row["entity_id"] for row in found} == {"sensor.a_temperature", "sensor.b_temperature"}
    assert len(run("entities_affected_by", kind="translation_key", key="temperature")) == 2
    naming = run("naming_for", entity_id="sensor.a_temperature")
    assert naming["entity_id"] == "sensor.a_temperature"
    assert naming["proposed_name"]


def test_a_rule_written_through_a_tool_changes_the_name(home):
    before = run("naming_for", entity_id="sensor.a_temperature")["proposed_name"]

    run("set_rule", kind="translation_key", key="temperature", value="Raumtemperatur")

    after = run("naming_for", entity_id="sensor.a_temperature")["proposed_name"]
    assert after != before
    assert "Raumtemperatur" in after
    assert run("list_rules")[0]["value"] == "Raumtemperatur"


def test_an_exception_beats_the_rule_for_one_entity(home):
    run("set_rule", kind="translation_key", key="temperature", value="Raumtemperatur")

    run("set_exception", entity_id="sensor.b_temperature", name="Fühler hinten")

    assert "Fühler hinten" in run("naming_for", entity_id="sensor.b_temperature")["proposed_name"]
    assert "Raumtemperatur" in run("naming_for", entity_id="sensor.a_temperature")["proposed_name"]


def test_deleting_a_rule_puts_the_supplied_name_back(home):
    rule = run("set_rule", kind="translation_key", key="temperature", value="Raumtemperatur")

    run("delete_rule", rule_id=rule["id"])

    assert run("list_rules") == []
    assert "Raumtemperatur" not in run("naming_for", entity_id="sensor.a_temperature")["proposed_name"]


def test_applying_writes_exactly_what_was_proposed(home, monkeypatch):
    """The tool must hand on the proposal, not a name it invented on the way."""
    run("set_rule", kind="translation_key", key="temperature", value="Raumtemperatur")
    proposed = run("naming_for", entity_id="sensor.a_temperature")
    written = {}

    async def record(old_entity_id, new_entity_id=None, friendly_name=None):
        written.update(old=old_entity_id, new=new_entity_id, name=friendly_name)
        return {"success": True}

    monkeypatch.setattr("naming_service.rename_entity", record)

    assert run("apply_naming", entity_id="sensor.a_temperature")["success"] is True
    assert written == {
        "old": "sensor.a_temperature",
        "new": proposed["proposed_entity_id"],
        "name": proposed["proposed_name"],
    }


def test_applying_to_an_unknown_entity_renames_nothing(home, monkeypatch):
    """An assistant that guesses an id must not hit a different entity."""
    touched = []

    async def record(*args, **kwargs):
        touched.append(args)
        return {"success": True}

    monkeypatch.setattr("naming_service.rename_entity", record)

    with pytest.raises(Exception):
        run("apply_naming", entity_id="sensor.does_not_exist")

    assert touched == []


def test_applying_an_exception_uses_the_exception(home, monkeypatch):
    """What set_exception decided has to be what apply_naming writes."""
    run("set_exception", entity_id="sensor.a_temperature", name="Fühler vorne")
    written = {}

    async def record(old_entity_id, new_entity_id=None, friendly_name=None):
        written.update(new=new_entity_id, name=friendly_name)
        return {"success": True}

    monkeypatch.setattr("naming_service.rename_entity", record)
    run("apply_naming", entity_id="sensor.a_temperature")

    assert "Fühler vorne" in written["name"]
    assert "fuhler_vorne" in written["new"] or "fühler_vorne" in written["new"]
