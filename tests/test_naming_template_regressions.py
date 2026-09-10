"""Regression tests for naming-template edge cases."""

import json

import pytest

from entity_restructurer import EntityRestructurer
from naming_overrides import NamingOverrides
from naming_templates import DEFAULT_TEMPLATES, NamingTemplateError, NamingTemplates


@pytest.fixture
def restructurer(tmp_path):
    """Restructurer with one area, one device and one device-main entity."""
    result = EntityRestructurer(
        client=object(),
        naming_overrides=NamingOverrides(str(tmp_path / "overrides.json")),
        type_mappings=None,
        naming_templates=NamingTemplates(str(tmp_path / "templates.json")),
    )
    result.floors = {}
    result.areas = {"office": {"area_id": "office", "name": "Büro"}}
    result.devices = {
        "device-1": {"id": "device-1", "name": "Homepod", "area_id": "office"},
    }
    result.entities = {
        "media_player.homepod": {
            "id": "registry-1",
            "entity_id": "media_player.homepod",
            "device_id": "device-1",
            "platform": "cast",
            "original_name": None,
            "has_entity_name": True,
        }
    }
    return result


def test_repeated_runs_do_not_grow_the_entity_name(restructurer):
    """Re-applying the template must not prepend the hierarchy again."""
    entity_id = "media_player.homepod"
    registry = restructurer.entities[entity_id]

    seen = []
    for _ in range(3):
        new_entity_id, new_name = restructurer.generate_new_entity_id(entity_id, registry)
        seen.append((new_entity_id, new_name))
        # Feed the result back in, the way a previous run would have stored it.
        registry["name"] = new_name

    assert len(set(seen)) == 1, f"name grew across runs: {seen}"


def test_device_without_any_name_does_not_raise(restructurer):
    """name_by_user, name and model may all be null."""
    restructurer.devices["device-1"].update({"name": None, "name_by_user": None, "model": None})

    context = restructurer.build_naming_context("media_player.homepod", restructurer.entities["media_player.homepod"])

    assert context["device"] == ""


def test_non_object_state_file_falls_back_to_defaults(tmp_path):
    """A JSON file whose top level is not an object must not break startup."""
    path = tmp_path / "templates.json"
    path.write_text(json.dumps([]), encoding="utf-8")

    assert NamingTemplates(str(path)).get_templates() == dict(DEFAULT_TEMPLATES)


@pytest.mark.parametrize(
    "templates",
    [
        {"device_name": 5, "entity_name": "{entity}", "entity_id": "{entity}"},
        [],
    ],
)
def test_malformed_templates_raise_naming_template_error(tmp_path, templates):
    """Bad payloads must surface as NamingTemplateError, not AttributeError."""
    manager = NamingTemplates(str(tmp_path / "templates.json"))

    with pytest.raises(NamingTemplateError):
        manager.set_templates(templates)
