"""Exceptions: one entity named its own way, and the ways out of drift.

An exception is the last word on a name, so the questions worth pinning down
are the ones about letting go of it again: which exceptions still change
anything, what happens to a name somebody set in Home Assistant itself, and
what "leave this one alone" actually leaves alone.
"""

import pytest

from entity_restructurer import EntityRestructurer
from naming_overrides import NamingOverrides
from naming_rules import NamingRules
from naming_state import NamingState
from naming_templates import NamingTemplates
from type_mappings import DEFAULT_SYSTEM_MAPPINGS, TypeMappings
import web_ui


@pytest.fixture
def client(tmp_path, monkeypatch):
    rules = NamingRules(
        str(tmp_path / "naming_rules.json"),
        device_class_keys=DEFAULT_SYSTEM_MAPPINGS["device_class"].keys(),
        default_language="de",
    )
    mappings = TypeMappings(user_mappings_path=str(tmp_path / "unused.json"), rules=rules)
    overrides = NamingOverrides(str(tmp_path / "overrides.json"))
    state = NamingState(str(tmp_path / "naming_state.json"))
    restructurer = EntityRestructurer(
        client=object(),
        naming_overrides=overrides,
        type_mappings=mappings,
        naming_templates=NamingTemplates(str(tmp_path / "templates.json")),
    )
    restructurer.floors = {}
    restructurer.areas = {"k": {"area_id": "k", "name": "Küche"}}
    restructurer.devices = {"d": {"id": "d", "name": "Deckenleuchte", "area_id": "k"}}
    restructurer.entities = {
        "sensor.a_temperature": {
            "id": "reg-a",
            "entity_id": "sensor.a_temperature",
            "device_id": "d",
            "platform": "mqtt",
            "original_name": "Temperature",
            "has_entity_name": True,
        },
    }
    monkeypatch.setitem(web_ui.renamer_state, "naming_rules", rules)
    monkeypatch.setitem(web_ui.renamer_state, "type_mappings", mappings)
    monkeypatch.setitem(web_ui.renamer_state, "naming_overrides", overrides)
    monkeypatch.setitem(web_ui.renamer_state, "naming_templates", restructurer.naming_templates)
    monkeypatch.setitem(web_ui.renamer_state, "naming_state", state)
    monkeypatch.setitem(web_ui.renamer_state, "restructurer", restructurer)
    web_ui.app.config["TESTING"] = True
    return web_ui.app.test_client(), overrides, state, restructurer


def test_an_exception_says_what_it_still_changes(client):
    api, overrides, _, restructurer = client
    overrides.set_entity_override("reg-a", "Fühler hinten")

    row = api.get("/api/naming/exceptions").get_json()["exceptions"][0]

    assert row["entity_id"] == "sensor.a_temperature"
    assert row["name"] == "Fühler hinten"
    assert row["source"] == "user"
    assert row["would_be"] and row["would_be"] != "Fühler hinten"
    assert row["redundant"] is False


def test_an_exception_the_rules_caught_up_with_is_marked(client):
    """It changes nothing any more, so it is only in the way."""
    api, overrides, _, restructurer = client
    would_be = restructurer.build_naming_context(
        "sensor.a_temperature", restructurer.entities["sensor.a_temperature"], ignore_exception=True
    )["entity"]
    overrides.set_entity_override("reg-a", would_be)

    row = api.get("/api/naming/exceptions").get_json()["exceptions"][0]

    assert row["redundant"] is True


def test_an_exception_without_an_entity_is_an_orphan(client):
    api, overrides, _, _ = client
    overrides.set_entity_override("reg-gone", "Irgendwas")

    row = api.get("/api/naming/exceptions").get_json()["exceptions"][0]

    assert row["orphan"] is True
    assert row["entity_id"] is None


def test_cleaning_up_keeps_everything_that_still_matters(client):
    api, overrides, _, restructurer = client
    would_be = restructurer.build_naming_context(
        "sensor.a_temperature", restructurer.entities["sensor.a_temperature"], ignore_exception=True
    )["entity"]
    overrides.set_entity_override("reg-a", would_be)
    overrides.set_entity_override("reg-gone", "Irgendwas")

    answer = api.post("/api/naming/exceptions/cleanup").get_json()

    assert answer["count"] == 2
    assert overrides.get_all_entity_overrides() == {}


def test_cleaning_up_leaves_a_working_exception_alone(client):
    api, overrides, _, _ = client
    overrides.set_entity_override("reg-a", "Fühler hinten")

    assert api.post("/api/naming/exceptions/cleanup").get_json()["count"] == 0
    assert overrides.get_entity_override("reg-a")["name"] == "Fühler hinten"


def test_a_name_set_in_home_assistant_can_be_adopted(client):
    """The stored name is the whole rendered one; an exception holds the type
    part, so adopting has to take it apart along the templates."""
    api, overrides, state, restructurer = client
    restructurer.entities["sensor.a_temperature"]["name"] = "Küche Deckenleuchte Fühler hinten"

    answer = api.post("/api/naming/exceptions/adopt", json={"registry_id": "reg-a"}).get_json()

    assert answer["name"] == "Fühler hinten"
    assert overrides.get_entity_override("reg-a")["source"] == "ha_ui"


def test_an_adopted_name_stops_counting_as_changed_elsewhere(client):
    api, _, state, restructurer = client
    entry = restructurer.entities["sensor.a_temperature"]
    entry["name"] = "Küche Deckenleuchte Fühler hinten"

    api.post("/api/naming/exceptions/adopt", json={"registry_id": "reg-a"})

    assert state.ownership(entry)["drift"] is False
    assert state.ownership(entry)["name_owner"] == "entity_manager"


def test_a_name_that_fits_no_template_is_adopted_as_it_stands(client):
    """Guessing would be worse than a value the user can see and correct."""
    api, overrides, _, restructurer = client
    restructurer.entities["sensor.a_temperature"]["name"] = "Kühlschrank"

    answer = api.post("/api/naming/exceptions/adopt", json={"registry_id": "reg-a"}).get_json()

    assert answer["name"] == "Kühlschrank"


def test_adopting_needs_a_name_to_adopt(client):
    api, _, _, _ = client

    assert api.post("/api/naming/exceptions/adopt", json={"registry_id": "reg-a"}).status_code == 400
    assert api.post("/api/naming/exceptions/adopt", json={"registry_id": "reg-nope"}).status_code == 404


def test_an_ignored_entity_keeps_what_its_integration_calls_it(client):
    api, overrides, _, restructurer = client
    api.post("/api/naming/exceptions/ignore", json={"registry_id": "reg-a"})

    context = restructurer.build_naming_context("sensor.a_temperature", restructurer.entities["sensor.a_temperature"])

    assert overrides.get_entity_override("reg-a")["keep_original"] is True
    assert context["entity"] == "Temperature"
    assert restructurer.last_resolutions["sensor.a_temperature"]["won_by"] == "ignored"


def test_an_ignored_entity_can_be_taken_back(client):
    api, overrides, _, _ = client
    api.post("/api/naming/exceptions/ignore", json={"registry_id": "reg-a"})

    api.post("/api/naming/exceptions/ignore", json={"registry_id": "reg-a", "ignore": False})

    assert overrides.get_entity_override("reg-a") is None


def test_asking_what_the_rules_alone_would_say_leaves_the_real_answer_standing(client):
    """The entity list reads the resolution back out of the restructurer, so a
    hypothetical question must not overwrite it."""
    api, overrides, _, restructurer = client
    overrides.set_entity_override("reg-a", "Fühler hinten")
    entry = restructurer.entities["sensor.a_temperature"]
    restructurer.build_naming_context("sensor.a_temperature", entry)

    restructurer.build_naming_context("sensor.a_temperature", entry, ignore_exception=True)

    assert restructurer.last_resolutions["sensor.a_temperature"]["won_by"] == "override"


def test_dropping_an_exception_hands_the_name_back_to_the_rules(client):
    api, overrides, _, _ = client
    overrides.set_entity_override("reg-a", "Fühler hinten")

    api.delete("/api/naming/exceptions/reg-a")

    assert overrides.get_entity_override("reg-a") is None
