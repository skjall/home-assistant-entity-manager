"""A supplied name is only ours to correct where it is a config entry's title.

A helper built in the Home Assistant interface takes its entity name from the
title of its config entry, and that title keeps the name of the day it was made.
Move the socket behind it and the helper still says "Kammer Lüftungsanlage
Steckdose Energie" in a kitchen. Everywhere else the supplied name comes out of
an integration's code and must not be touched.
"""

import supplied_names

MOVED = {
    "entity_id": "sensor.kuche_kuhlschrank_steckdose_energie",
    "name": "Küche Kühlschrank Steckdose Energie",
    "original_name": "Kammer Lüftungsanlage Steckdose Energie",
    "config_entry_id": "abc",
}
ITS_ENTRY = {"entry_id": "abc", "domain": "integration", "title": "Kammer Lüftungsanlage Steckdose Energie"}


def test_a_helper_named_by_its_title_can_be_corrected():
    answer = supplied_names.what_to_correct(MOVED, ITS_ENTRY, siblings=1)

    assert answer["title_should_be"] == "Küche Kühlschrank Steckdose Energie"
    assert answer["entry_id"] == "abc"
    assert answer["domain"] == "integration"


def test_a_name_from_an_integration_is_left_alone():
    """Miele's "Status" is in its translation files, not in any title."""
    entity = {"name": "Küche Kühlschrank Status", "original_name": "Status", "config_entry_id": "abc"}
    entry = {"entry_id": "abc", "domain": "miele", "title": "Miele@home"}

    assert supplied_names.what_to_correct(entity, entry, siblings=1) is None


def test_an_entry_with_several_entities_is_left_alone():
    """One title cannot be the name of two entities."""
    assert supplied_names.what_to_correct(MOVED, ITS_ENTRY, siblings=4) is None


def test_an_entity_with_no_name_of_its_own_has_nothing_to_write():
    entity = {**MOVED, "name": None}

    assert supplied_names.what_to_correct(entity, ITS_ENTRY, siblings=1) is None


def test_a_name_that_already_agrees_needs_no_correction():
    entity = {**MOVED, "name": "Kammer Lüftungsanlage Steckdose Energie"}

    assert supplied_names.what_to_correct(entity, ITS_ENTRY, siblings=1) is None


def test_spelling_alone_is_not_a_difference():
    """Otherwise a rename would be offered for a comma or a case change."""
    entity = {**MOVED, "name": "kammer lüftungsanlage steckdose energie"}

    assert supplied_names.what_to_correct(entity, ITS_ENTRY, siblings=1) is None


def test_an_entity_without_an_entry_is_left_alone():
    assert supplied_names.what_to_correct(MOVED, None, siblings=1) is None
