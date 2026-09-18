"""Taking a name apart when the template has a field this entity does not fill.

An automation has no device, so "{area} {device} {entity}" renders "Badezimmer
Heizungssteuerung" with one space where the template has two. Reading it back
looked for the two, found nothing, and fell through to the bare "{entity}",
which answers with the whole name - area and all. The area was then rendered in
front of it again and the proposal read "Badezimmer Badezimmer
Heizungssteuerung".

Taking a name apart is rendering it backwards, so it has to allow for exactly
what rendering does with an empty field.
"""

import pytest

from naming_templates import NamingTemplates

WITH_DEVICE = {"area": "Küche", "device": "Kühlschrank", "entity": "Temperatur"}
WITHOUT = {"area": "Badezimmer", "device": "", "entity": "Heizungssteuerung"}


@pytest.fixture
def templates(tmp_path):
    return NamingTemplates(str(tmp_path / "naming_templates.json"))


def test_a_name_that_never_had_a_device_gives_its_type_back(templates):
    assert templates.extract_field("entity_name", "Badezimmer Heizungssteuerung", "entity", WITHOUT) == (
        "Heizungssteuerung"
    )


def test_the_name_it_renders_is_the_name_it_reads(templates):
    """The two are one operation in opposite directions."""
    rendered = templates.render("entity_name", WITHOUT)

    assert rendered == "Badezimmer Heizungssteuerung"
    assert templates.extract_field("entity_name", rendered, "entity", WITHOUT) == "Heizungssteuerung"


def test_a_name_with_every_field_still_comes_apart(templates):
    rendered = templates.render("entity_name", WITH_DEVICE)

    assert rendered == "Küche Kühlschrank Temperatur"
    assert templates.extract_field("entity_name", rendered, "entity", WITH_DEVICE) == "Temperatur"


def test_a_type_of_several_words_is_kept_whole(templates):
    context = {**WITHOUT, "entity": "Heizung im Bad"}

    assert templates.extract_field("entity_name", "Badezimmer Heizung im Bad", "entity", context) == "Heizung im Bad"


def test_a_name_the_active_template_cannot_account_for_is_read_as_a_type(templates):
    """Home Assistant's own scheme is "{entity}", and names written under it are
    a bare type. It stays available for names the active template cannot
    account for - which is right, and was reached far too easily: it used to
    answer for names the active template describes exactly.
    """
    assert templates.extract_field("entity_name", "Küche Heizungssteuerung", "entity", WITHOUT) == (
        "Küche Heizungssteuerung"
    )


def test_the_active_template_is_asked_before_that_fallback(templates):
    """The name this entity would be given comes apart along its own template."""
    assert templates.extract_field("entity_name", "Badezimmer Heizungssteuerung", "entity", WITHOUT) == (
        "Heizungssteuerung"
    )


def test_the_device_can_be_read_out_of_a_device_name(templates):
    assert templates.extract_field("device_name", "Küche Kühlschrank", "device", WITH_DEVICE) == "Kühlschrank"


def test_a_name_carrying_the_area_twice_reads_the_second_one_as_the_type(templates):
    """What the old proposal wrote is still read back for what it says."""
    assert templates.extract_field("entity_name", "Badezimmer Badezimmer Heizungssteuerung", "entity", WITHOUT) == (
        "Badezimmer Heizungssteuerung"
    )
