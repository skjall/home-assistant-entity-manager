"""A name a template finishes is not an entity id.

A script that targets `input_boolean.negativstrom_{{ dev.name | lower }}_aktiv`
names a helper per device, worked out when it runs. Read as written, the target
was taken for an id and the helper it stands for was reported missing - twice
over, because a string holding the same template matched as far as the brace and
`input_boolean.negativstrom_` was reported as well.

An id inside a template is a different thing: `states('sensor.real')` names an
entity that exists, and the scan should keep finding it.
"""

import pytest

from reference_checker import ReferenceChecker


@pytest.fixture
def checker():
    return ReferenceChecker("http://ha.invalid", "token")


def found_in(checker, data):
    return set(checker._extract_entity_ids_with_path(data))


def test_a_target_a_template_finishes_is_not_read_as_an_id(checker):
    data = {"target": {"entity_id": "input_boolean.negativstrom_{{ dev.name | lower }}_aktiv"}}

    assert found_in(checker, data) == set()


def test_a_list_of_targets_drops_only_the_templated_one(checker):
    data = {"entity_id": ["input_text.negativstrom_{{ dev.name }}_titel", "input_text.real_helper"]}

    assert found_in(checker, data) == {"input_text.real_helper"}


def test_a_string_is_not_cut_off_at_the_brace(checker):
    """`input_boolean.negativstrom_` is half a name, not a helper that vanished."""
    data = {"variables": {"session_flag": "input_boolean.negativstrom_{{ dev.name | lower }}_aktiv"}}

    assert found_in(checker, data) == set()


def test_a_name_a_template_begins_is_not_read_as_an_id(checker):
    data = {"variables": {"flag": "input_boolean.{{ room }}_aktiv"}}

    assert "input_boolean.aktiv" not in found_in(checker, data)


def test_an_id_inside_a_template_is_still_found(checker):
    data = {"value_template": "{{ states('sensor.real') }}"}

    assert found_in(checker, data) == {"sensor.real"}


def test_an_id_next_to_a_template_is_still_found(checker):
    data = {"message": "{{ now() }} sensor.real reported"}

    assert found_in(checker, data) == {"sensor.real"}


def test_a_plain_target_is_unchanged(checker):
    data = {"entity_id": "switch.kammer_it_steckdose_schalter"}

    assert found_in(checker, data) == {"switch.kammer_it_steckdose_schalter"}


def test_a_service_name_in_a_target_is_still_ignored(checker):
    data = {"entity_id": "automation.turn_on"}

    assert found_in(checker, data) == set()
