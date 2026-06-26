"""Tests for the central, word-boundary-safe entity reference replacement."""

from entity_ref_utils import (
    extract_entity_ids,
    replace_entity_in_obj,
    replace_entity_ref_in_string,
)


def test_replace_exact_value():
    assert replace_entity_ref_in_string("light.old", "light.old", "light.new") == ("light.new", True)


def test_replace_no_match():
    assert replace_entity_ref_in_string("light.other", "light.old", "light.new") == ("light.other", False)


def test_template_word_boundary_does_not_overmatch():
    # sensor.temp must NOT match inside sensor.temperature
    result = replace_entity_ref_in_string("{{ states('sensor.temperature') }}", "sensor.temp", "sensor.x")
    assert result == ("{{ states('sensor.temperature') }}", False)


def test_template_match():
    value, changed = replace_entity_ref_in_string("{{ states('sensor.temp') }}", "sensor.temp", "sensor.x")
    assert changed
    assert "sensor.x" in value
    assert "sensor.temp'" not in value


def test_plain_freetext_is_untouched():
    # Not a template -> leave plain text alone even if the id appears as a substring
    result = replace_entity_ref_in_string("The sensor.temp is warm", "sensor.temp", "sensor.x")
    assert result == ("The sensor.temp is warm", False)


def test_replace_in_obj_nested_in_place():
    data = {"a": "light.old", "b": ["x", "light.old"], "c": {"d": "light.old"}}
    changed = replace_entity_in_obj(data, "light.old", "light.new")
    assert changed is True
    assert data == {"a": "light.new", "b": ["x", "light.new"], "c": {"d": "light.new"}}


def test_replace_in_obj_no_change():
    data = {"a": "light.other"}
    assert replace_entity_in_obj(data, "light.old", "light.new") is False
    assert data == {"a": "light.other"}


def test_extract_entity_ids_from_dashboard_like_structure():
    cfg = {
        "views": [{"cards": [{"type": "entities", "entities": ["sensor.a", {"entity": "light.b"}]}]}],
        "x": "{{ states('sensor.c') }}",
    }
    ids = extract_entity_ids(cfg)
    assert {"sensor.a", "light.b", "sensor.c"} <= ids
