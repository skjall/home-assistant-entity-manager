"""A tag that was never closed swallows the markup after it.

`<div class="drift-bar"` without its ">" turned the following `<span` into
attributes of the div, so the line inside it was rendered outside its own box -
which looked like a styling problem and was not.
"""

import os
import re

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = ("index.html", "settings.html")
# Their contents are JavaScript and CSS, where "<" is just a character.
SKIP = re.compile(r"<(script|style)\b.*?</\1>", re.DOTALL | re.IGNORECASE)


def unclosed_tags(markup: str) -> list:
    """Every tag that is still open when the next one starts."""
    markup = SKIP.sub(lambda match: "\n" * match.group(0).count("\n"), markup)
    found = []
    line = 1
    index = 0
    while index < len(markup):
        char = markup[index]
        if char == "\n":
            line += 1
            index += 1
            continue
        if char != "<":
            index += 1
            continue
        started = line
        index += 1
        quote = ""
        while index < len(markup):
            char = markup[index]
            if char == "\n":
                line += 1
            if quote:
                if char == quote:
                    quote = ""
            elif char in "\"'":
                quote = char
            elif char == ">":
                break
            elif char == "<":
                found.append(started)
                break
            index += 1
        index += 1
    return found


@pytest.mark.parametrize("name", TEMPLATES)
def test_no_tag_is_left_open(name):
    with open(os.path.join(HERE, "templates", name), encoding="utf-8") as handle:
        markup = handle.read()

    open_at = unclosed_tags(markup)

    assert not open_at, f"{name} has a tag left open at line(s): {open_at}"


def test_the_check_notices_a_missing_bracket():
    """Otherwise a passing test would say nothing at all."""
    assert unclosed_tags('<div class="a"\n  <span>hi</span>\n</div>') == [1]


def test_a_less_than_inside_an_attribute_is_not_a_tag():
    assert unclosed_tags('<span x-show="a < b">ok</span>') == []
