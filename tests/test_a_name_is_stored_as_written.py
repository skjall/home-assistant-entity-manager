"""A display name is data, not markup.

It goes into Home Assistant's registry and is read back by Home Assistant, by
every automation and by every dashboard. Escaping it on the way in does not
protect anything - it renames the thing. A coffee machine's "Calc'n'Clean in 5
Tassen" was stored as "Calc&#x27;n&#x27;Clean in 5 Tassen" and read that way
everywhere afterwards.
"""

import pytest

from sanitize import sanitize_name


@pytest.mark.parametrize(
    "written",
    [
        "Küche Kaffeevollautomat Calc'n'Clean in 5 Tassen",
        'Büro "Ablage" Temperatur',
        "Küche Herd & Backofen",
        "Balkon <Tank> Füllstand",
        "Sonne = hell",
    ],
)
def test_a_name_survives_being_stored(written):
    assert sanitize_name(written) == written


def test_control_characters_are_still_removed():
    """They are not text and Home Assistant would not take them."""
    assert sanitize_name("Küche\x00 Herd\x07") == "Küche Herd"


def test_a_name_is_still_cut_to_length():
    assert len(sanitize_name("a" * 400)) == 255


def test_surrounding_space_is_still_dropped():
    assert sanitize_name("  Küche Herd  ") == "Küche Herd"


def test_nothing_stays_nothing():
    assert sanitize_name(None) is None
