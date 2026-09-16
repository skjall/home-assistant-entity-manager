"""A label the templates ask for exists in every language.

A missing key shows as an empty tooltip or a blank line - nothing errors, so it
survives until somebody notices the gap. This is what noticed the empty title on
the link that opens an area in Home Assistant.
"""

import json
import os
import re

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANGUAGES = ("de", "en", "fr", "es")
# t('settings.access_state_' + state) names a family, not a key; the members are
# checked where they are built.
PREFIXES = ("settings.access_mode_", "settings.access_state_", "settings.group_", "settings.pick_")

ASKED_FOR = re.compile(r"""t\(\s*['"]([a-z0-9_]+\.[a-z0-9_]+)['"]""")


def keys_used() -> set:
    used = set()
    for name in ("index.html", "settings.html"):
        with open(os.path.join(HERE, "templates", name), encoding="utf-8") as handle:
            used |= set(ASKED_FOR.findall(handle.read()))
    return {key for key in used if not key.endswith("_") and key not in PREFIXES}


def words_of(language: str) -> set:
    with open(os.path.join(HERE, "translations/ui", language + ".json"), encoding="utf-8") as handle:
        data = json.load(handle)
    return {f"{section}.{key}" for section, words in data.items() if isinstance(words, dict) for key in words}


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_label_the_templates_ask_for_has_words(language):
    missing = sorted(keys_used() - words_of(language))

    assert not missing, f"{language} has no words for: {', '.join(missing)}"


def test_the_languages_say_the_same_things():
    """A key only some languages have leaves the others showing nothing."""
    everywhere = set.intersection(*(words_of(language) for language in LANGUAGES))
    anywhere = set.union(*(words_of(language) for language in LANGUAGES))

    assert not sorted(anywhere - everywhere)
