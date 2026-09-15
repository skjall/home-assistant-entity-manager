"""Helpers built in the Home Assistant interface, and their free text.

A helper's entity fields are carried along by Home Assistant itself. Its
templates are not: an entity id inside a Jinja string is prose, and a rename
leaves it pointing at nothing. Reading and writing those options goes through
an options flow, which has two rules worth pinning down - a flow that is only
read must be aborted, and a field without a value must be left out of a write,
or Home Assistant refuses the whole thing.
"""

import asyncio
import json

import pytest

from entity_ref_utils import replace_entity_ref_in_string
from helper_options import HelperOptions

TEMPLATE = "{% set p = states('sensor.vorrat_steckdose_leistung') | float(0) %}\n{{ p * 2 }}"
OPTIONS = {
    "state": TEMPLATE,
    "unit_of_measurement": "EUR/h",
    "device_class": None,
    "additional_options": None,
}


class FakeHomeAssistant:
    """The four calls a helper's options need, and a memory of what was asked."""

    def __init__(self, options=None, entries=None):
        self.options = dict(options if options is not None else OPTIONS)
        self.entries = (
            entries
            if entries is not None
            else [
                {"entry_id": "e1", "domain": "template", "title": "Vorrat Kosten", "supports_options": True},
                {"entry_id": "e2", "domain": "hue", "title": "Hue Bridge", "supports_options": True},
                {
                    "entry_id": "e3",
                    "domain": "template",
                    "title": "Aus",
                    "supports_options": True,
                    "disabled_by": "user",
                },
            ]
        )
        self.calls = []
        self.submitted = None

    async def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path == "/api/config/config_entries/entry":
            return self.entries
        if path == "/api/config/config_entries/options/flow":
            return {
                "flow_id": "f1",
                "type": "form",
                "data_schema": [
                    {"name": name, "description": {"suggested_value": value}} for name, value in self.options.items()
                ],
            }
        if path == "/api/config/config_entries/options/flow/f1" and method == "POST":
            self.submitted = payload
            self.options.update(payload)
            return {"type": "create_entry"}
        return None


@pytest.fixture
def helpers():
    fake = FakeHomeAssistant()
    built = HelperOptions("http://ha", "token")
    built._request = fake.request
    return built, fake


def test_a_flow_that_was_only_read_is_given_back(helpers):
    """Otherwise it sits in Home Assistant's list of half-finished dialogs."""
    built, fake = helpers

    asyncio.run(built.options_of("e1"))

    assert ("DELETE", "/api/config/config_entries/options/flow/f1", None) in fake.calls


def test_only_helpers_that_can_hold_free_text_are_looked_at(helpers):
    """A real integration's options flow is nobody's business here, and a
    disabled helper has nothing to answer with."""
    built, _ = helpers

    entries = asyncio.run(built.entries())

    assert [entry["entry_id"] for entry in entries] == ["e1"]


def test_a_rename_reaches_into_the_template(helpers):
    built, fake = helpers

    result = asyncio.run(built.rename("sensor.vorrat_steckdose_leistung", "sensor.kuche_vaporisator_leistung"))

    assert result == {"success": ["Vorrat Kosten"], "failed": []}
    assert "sensor.kuche_vaporisator_leistung" in fake.options["state"]
    assert "sensor.vorrat_steckdose_leistung" not in fake.options["state"]


def test_a_write_leaves_out_what_has_no_value(helpers):
    """Sent as null they are refused, and one refused field fails the write."""
    built, fake = helpers

    asyncio.run(built.rename("sensor.vorrat_steckdose_leistung", "sensor.kuche_vaporisator_leistung"))

    assert "device_class" not in fake.submitted
    assert "additional_options" not in fake.submitted
    assert fake.submitted["unit_of_measurement"] == "EUR/h"


def test_a_helper_nobody_renamed_is_left_alone(helpers):
    built, fake = helpers

    result = asyncio.run(built.rename("sensor.something_else", "sensor.whatever"))

    assert result == {"success": [], "failed": []}
    assert fake.submitted is None


def test_a_dead_reference_is_reported_with_the_field_it_sits_in(helpers):
    built, _ = helpers

    broken = asyncio.run(built.broken({"sensor.kuche_vaporisator_leistung", "light.a"}))

    assert broken == [
        {
            "entry_id": "e1",
            "title": "Vorrat Kosten",
            "domain": "template",
            "field": "state",
            "missing_entity_id": "sensor.vorrat_steckdose_leistung",
        }
    ]


def test_prose_that_only_looks_like_an_entity_is_not_reported():
    """A template is free text; not every dotted word names an entity."""
    fake = FakeHomeAssistant(options={"state": "{{ 1 }} siehe readme.txt und config.yaml"})
    built = HelperOptions("http://ha", "token")
    built._request = fake.request

    assert asyncio.run(built.broken({"sensor.a"})) == []


def test_a_helper_that_opens_with_a_menu_is_left_alone():
    """Filling in a step nobody looked at would be guessing."""
    fake = FakeHomeAssistant()

    async def menu(method, path, payload=None):
        if path == "/api/config/config_entries/options/flow":
            return {"flow_id": "f1", "type": "menu"}
        return await fake.request(method, path, payload)

    built = HelperOptions("http://ha", "token")
    built._request = menu

    assert asyncio.run(built.options_of("e1")) == {}


def test_the_options_of_one_helper_are_read_once(helpers):
    """A device rename asks for every one of its entities in turn."""
    built, fake = helpers

    asyncio.run(built.options_of("e1"))
    asyncio.run(built.options_of("e1"))

    started = [call for call in fake.calls if call[1] == "/api/config/config_entries/options/flow"]
    assert len(started) == 1


def test_a_set_block_counts_as_a_template():
    """Helpers from the interface often fetch their value with `{% set %}`."""
    value = "{% set p = states('sensor.alt') %}"

    replaced, changed = replace_entity_ref_in_string(value, "sensor.alt", "sensor.neu")

    assert changed and replaced == "{% set p = states('sensor.neu') %}"


def test_an_entity_whose_id_starts_the_same_is_not_caught():
    value = "{{ states('sensor.alt_gross') }}"

    _, changed = replace_entity_ref_in_string(value, "sensor.alt", "sensor.neu")

    assert changed is False


def test_a_helper_that_refuses_the_write_does_not_stop_the_others():
    fake = FakeHomeAssistant(
        entries=[
            {"entry_id": "e1", "domain": "template", "title": "Kaputt", "supports_options": True},
            {"entry_id": "e2", "domain": "template", "title": "Geht", "supports_options": True},
        ]
    )

    async def refuse_first(method, path, payload=None):
        if path == "/api/config/config_entries/options/flow/f1" and method == "POST" and refuse_first.first:
            refuse_first.first = False
            return {"type": "form", "errors": {"state": "invalid_template"}}
        return await fake.request(method, path, payload)

    refuse_first.first = True
    built = HelperOptions("http://ha", "token")
    built._request = refuse_first

    result = asyncio.run(built.rename("sensor.vorrat_steckdose_leistung", "sensor.neu"))

    assert result["failed"] == ["Kaputt"]
    assert result["success"] == ["Geht"]


def test_what_a_helper_says_stays_data_not_code(helpers):
    """The template is carried through untouched apart from the id."""
    built, fake = helpers

    asyncio.run(built.rename("sensor.vorrat_steckdose_leistung", "sensor.neu"))

    assert json.loads(json.dumps(fake.submitted["state"])).startswith("{% set p = states('sensor.neu')")
