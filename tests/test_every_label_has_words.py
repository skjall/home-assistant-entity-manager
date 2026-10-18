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
STEP_LABEL = re.compile(r"""label:\s*["']([a-z0-9_]+)["']""")
# Quoted either way, and a digit is part of a name: a step called AREA2 that no
# label covers would otherwise go unseen by the very test that looks for that.
STEP_LOGGED = re.compile(r"""\.log\(\s*["']([A-Z][A-Z0-9_]*)["']""")
STEP_NAMED = re.compile(r"""^\s*([A-Z][A-Z0-9_]*)\s*:""", re.M)
# Every step but this one belongs to a job the interface shows. STEP is what the
# job machinery itself writes in its own tests.
NOT_SHOWN = {"STEP"}
# Steps the add-on has logged for as long as the catalogue has existed. They are
# named here so a regex that stops matching fails this test rather than passing it
# on one lucky hit: "something was found" is true of a near-total miss as well.
ALWAYS_LOGGED = {"RENAME", "ENABLE", "ERROR"}
# Where the add-on's own code lives. Its tests log steps of their own, and the
# frontend and the build output are not Python.
NOT_THE_ADD_ON = {"tests", "node_modules", "static", "src", "scripts"}


def catalogue_source() -> str:
    """The jobSteps object, read brace by brace rather than by how it is indented.

    Reformatting config.js - two spaces for four, a tab - must not quietly hand
    this test half a catalogue to check.
    """
    with open(os.path.join(HERE, "src/js/config.js"), encoding="utf-8") as handle:
        source = handle.read()
    opened = source.index("{", source.index("jobSteps:"))
    depth = 0
    for at in range(opened, len(source)):
        if source[at] == "{":
            depth += 1
        elif source[at] == "}":
            depth -= 1
            if depth == 0:
                return source[opened : at + 1]
    raise AssertionError("the jobSteps catalogue in src/js/config.js is not closed")


def steps_the_log_knows() -> dict:
    """The catalogue the progress panel and the settings log both read."""
    listed = catalogue_source()
    names = STEP_NAMED.findall(listed)
    labels = STEP_LABEL.findall(listed)
    if len(names) != len(labels):
        raise AssertionError(f"every step needs one label: read {names} and {labels}")
    return dict(zip(names, labels))


def steps_the_add_on_logs() -> set:
    """Every step name written by the add-on's own modules, wherever they sit."""
    logged = set()
    for path in Path(HERE).rglob("*.py"):
        if set(path.relative_to(HERE).parts[:-1]) & NOT_THE_ADD_ON or path.name.startswith("."):
            continue
        logged |= set(STEP_LOGGED.findall(path.read_text(encoding="utf-8")))
    return logged


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_step_of_a_job_has_words(language):
    """A step with no label is shown as itself - untranslated, in capitals."""
    labels = steps_the_log_knows()
    missing = sorted(step for step, label in labels.items() if f"jobs.{label}" not in words_of(language))

    assert not missing, f"{language} has no words for: {', '.join(missing)}"


def test_every_step_a_job_logs_is_one_the_log_knows():
    """Unknown steps are not an error anywhere: the panel falls back to the step's
    own name, so the gap only shows as a raw "RENAMED" in front of the user."""
    logged = steps_the_add_on_logs()

    # The steps are found by reading the source, so a regex that has stopped
    # matching must fail here rather than leave the check below with nothing to do.
    assert ALWAYS_LOGGED <= logged, f"the add-on logs more than {sorted(logged)} - this test has gone blind"

    unknown = sorted(logged - NOT_SHOWN - set(steps_the_log_knows()))
    assert not unknown, f"no label in src/js/config.js for: {', '.join(unknown)}"
