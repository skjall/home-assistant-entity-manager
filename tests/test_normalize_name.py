"""Tests for entity-ID name normalization.

Home Assistant derives entity IDs via ``python-slugify`` (see
``homeassistant.util.slugify``), which transliterates non-ASCII characters to
their closest ASCII equivalents instead of stripping them. ``normalize_name``
must produce the same slugs so renamed entities match what HA would generate.

The expected values below were provided in issue #58 by reproducing HA's
"Recreate entity IDs" for several languages.
"""

import pytest

from hierarchy_manager import normalize_name


@pytest.mark.parametrize(
    "text, expected",
    [
        # Czech
        (
            "Příliš žluťoučký kůň úpěl ďábelské ódy",
            "prilis_zlutoucky_kun_upel_dabelske_ody",
        ),
        # Polish
        ("Zażółć gęślą jaźń", "zazolc_gesla_jazn"),
        # Hungarian
        ("Árvíztűrő tükörfúrógép", "arvizturo_tukorfurogep"),
        # German (umlauts + ß)
        (
            "Falsches Üben von Xylophonmusik quält jeden größeren Zwerg.",
            "falsches_uben_von_xylophonmusik_qualt_jeden_grosseren_zwerg",
        ),
        # French (accents + apostrophe)
        (
            "L'épagneul français a hâte de manger son gâteau sur le canapé déjà usé.",
            "l_epagneul_francais_a_hate_de_manger_son_gateau_sur_le_canape_deja_use",
        ),
    ],
)
def test_transliterates_non_ascii(text: str, expected: str) -> None:
    """Accented characters are transliterated, matching HA's entity IDs."""
    assert normalize_name(text) == expected


def test_german_umlauts() -> None:
    """The original German-only behaviour is preserved (ä/ö/ü/ß)."""
    assert normalize_name("Wohnzimmer Büro") == "wohnzimmer_buro"
    assert normalize_name("Straße") == "strasse"


def test_collapses_and_trims_separators() -> None:
    """Runs of unsupported characters collapse to a single underscore."""
    assert normalize_name("  Hello --- World!!  ") == "hello_world"


@pytest.mark.parametrize("value", ["", None])
def test_empty_input_returns_empty_string(value) -> None:
    """Empty/None input yields an empty string (not HA's ``unknown``)."""
    assert normalize_name(value) == ""


def test_input_without_usable_characters_returns_empty_string() -> None:
    """Input that slugifies to nothing yields an empty string."""
    assert normalize_name("---") == ""
