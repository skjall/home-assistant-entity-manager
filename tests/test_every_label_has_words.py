"""A label the templates ask for exists in every language.

A missing key shows as an empty tooltip or a blank line - nothing errors, so it
survives until somebody notices the gap. This is what noticed the empty title on
the link that opens an area in Home Assistant.
"""

import json
import os
from pathlib import Path
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


# The step labels are asked for as t('jobs.' + step.label), which the regex above
# cannot see - which is how a step the job logs came to be shown to the user as its
# own raw name, "RENAMED" beside "Umbenannt".
STEP_LABEL = re.compile(r'label:\s*"([a-z0-9_]+)"')
STEP_LOGGED = re.compile(r'\.log\(\s*"([A-Z][A-Z_]+)"')
# Every step but this one belongs to a job the interface shows. STEP is what the
# job machinery itself writes in its own tests.
NOT_SHOWN = {"STEP"}


def steps_the_log_knows() -> dict:
    """The catalogue the progress panel and the settings log both read."""
    with open(os.path.join(HERE, "src/js/config.js"), encoding="utf-8") as handle:
        catalogue = handle.read()
    # To the brace that closes the catalogue, not to the first one inside it:
    # every step is written as a braced object of its own.
    at = catalogue.index("jobSteps: {")
    listed = catalogue[at : catalogue.index("\n    }", at)]
    names = re.findall(r"^\s*([A-Z][A-Z_]*):", listed, re.M)
    labels = STEP_LABEL.findall(listed)
    assert len(names) == len(labels), (names, labels)
    return dict(zip(names, labels))


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_step_of_a_job_has_words(language):
    """A step with no label is shown as itself - untranslated, in capitals."""
    asked = {f"jobs.{label}" for label in steps_the_log_knows().values()}

    assert not sorted(asked - words_of(language)), f"{language} has no words for a step"


def test_every_step_a_job_logs_is_one_the_log_knows():
    """Unknown steps are not an error anywhere: the panel falls back to the step's
    own name, so the gap only shows as a raw "RENAMED" in front of the user."""
    logged = set()
    for path in Path(HERE).glob("*.py"):
        logged |= set(STEP_LOGGED.findall(path.read_text(encoding="utf-8")))

    assert logged - NOT_SHOWN, "the steps are found by reading the source, so finding none is a broken test"
    assert not sorted(logged - NOT_SHOWN - set(steps_the_log_knows()))
