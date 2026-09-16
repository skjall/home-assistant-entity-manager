"""A proposal must not say less than the name already on the entity.

Miele calls two fridge sensors "Temperatur" and "Temperaturzone 2". The names
standing on them say "Küche Kühlschrank Temperatur Kühlzone" and "Küche
Kühlschrank Temperaturzone Gefrierzone", and the zone is the only thing telling
the two apart. Proposing the supplied name would have thrown it away and left a
bare counter behind.
"""

import pytest

from entity_restructurer import EntityRestructurer


@pytest.fixture
def restructurer():
    return EntityRestructurer.__new__(EntityRestructurer)


def test_a_name_that_adds_a_word_says_more(restructurer):
    assert restructurer._says_more("Temperatur Kühlzone", "Temperatur") is True


def test_a_rewording_does_not_count_as_saying_more(restructurer):
    """Otherwise every differently worded name would outrank the rule."""
    assert restructurer._says_more("Stromstärke", "Strom") is False


def test_the_same_name_says_no_more(restructurer):
    assert restructurer._says_more("Temperatur", "Temperatur") is False


def test_a_shorter_name_says_less(restructurer):
    assert restructurer._says_more("Temperatur", "Temperatur Kühlzone") is False


def test_a_bare_counter_is_not_a_word(restructurer):
    """"Temperaturzone 2" numbers what it cannot name; the zone names it."""
    assert restructurer._says_more("Temperaturzone Gefrierzone", "Temperaturzone 2") is True


def test_a_word_the_supplied_name_alone_has_settles_it(restructurer):
    assert restructurer._says_more("Batterie Ladezustand", "Ladezustand Prozent") is False


def test_a_name_still_carrying_its_device_is_spotted(restructurer):
    """Unwinding answers with the whole name when it does not recognise the
    template, and that name would be rendered with its prefix a second time."""
    assert restructurer._carries_prefix("Jan Grossheim Gewicht", ("Jan Grossheim",)) is True


def test_a_stripped_name_carries_no_prefix(restructurer):
    assert restructurer._carries_prefix("Temperatur Kühlzone", ("Küche", "Kühlschrank")) is False


def test_an_empty_prefix_matches_nothing(restructurer):
    assert restructurer._carries_prefix("Temperatur", ("", None)) is False
