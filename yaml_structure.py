#!/usr/bin/env python3
"""Which object in a YAML file a given line belongs to.

A line number tells the user where to go but not what they are looking at.
"/config/packages/water.yaml:470" is an address; "Prime the pump
(script.balkon_pumpe_priming)" is the thing itself.

Home Assistant's YAML carries tags no plain parser knows - !include, !secret,
!env_var - so every unknown tag is read as an ordinary value. `yaml.compose`
then answers with a node tree in which every key remembers the line it starts
on, and the object holding a line is the path down to it.

Nothing here knows a file name, a language or a domain list: an installation
that keeps its automations somewhere else, in another language, under another
layout, is read the same way. What cannot be determined is left empty rather
than guessed, and a file that does not parse falls back to the line alone.
"""

import logging
import os
from typing import Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

# Where an object states what it is called, in the order a reader would accept.
# `alias` is what automations and scripts use, `title` what a dashboard view
# uses, `name` what most helpers and template entities use.
NAME_FIELDS = ("alias", "friendly_name", "name", "title")

# The id an object carries when it has no name of its own.
ID_FIELDS = ("id", "unique_id")

# A file this large is parsed only when something was found in it.
_TOO_LARGE_TO_PARSE = 8 * 1024 * 1024


class Forgiving(yaml.SafeLoader):
    """Reads Home Assistant's YAML without knowing its tags."""


def _whatever(loader, suffix, node):  # noqa: ARG001 - the tag itself does not matter
    """Any tagged value, as the plain value underneath it."""
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


Forgiving.add_multi_constructor("!", _whatever)


class Where:
    """What holds a line, as far as the file says.

    `object_id` is filled only where the structure actually names one: a mapping
    of object ids under a domain key, as `script:` uses. A list of automations
    names none, and a dashboard names none either - those carry a `name` and a
    `trail` and nothing more.
    """

    __slots__ = ("trail", "name", "object_id")

    def __init__(self, trail: List[str], name: Optional[str] = None, object_id: Optional[str] = None):
        self.trail = trail
        self.name = name
        self.object_id = object_id

    def to_dict(self) -> Dict:
        return {"trail": " → ".join(self.trail), "name": self.name, "object_id": self.object_id}

    def described(self) -> str:
        """One line for a log: the name, the id, or the path down to it."""
        if self.name and self.object_id:
            return f"{self.name} ({self.object_id})"
        return self.name or self.object_id or " → ".join(self.trail)

    def __repr__(self) -> str:  # pragma: no cover - for test output
        return f"Where({self.described()})"


def _scalar_under(node, keys) -> Optional[str]:
    """The first of `keys` this mapping holds as a plain string."""
    if not isinstance(node, yaml.MappingNode):
        return None
    held = {key.value: value for key, value in node.value if isinstance(key, yaml.ScalarNode)}
    for key in keys:
        found = held.get(key)
        if isinstance(found, yaml.ScalarNode) and found.value:
            return str(found.value)
    return None


def _holds(node, line: int) -> bool:
    """Whether a node covers this line. Lines count from one, marks from zero."""
    return node.start_mark.line <= line - 1 <= node.end_mark.line


def _descend(node, line: int, trail: List[Tuple[str, object]]) -> List[Tuple[str, object]]:
    """The longest path of keys whose span still holds the line.

    Spans touch at their edges, so the deepest match is taken rather than the
    first: a line that ends one block and begins the next belongs to the one it
    is written inside.
    """
    if not _holds(node, line):
        return []
    best = trail
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            if not isinstance(key, yaml.ScalarNode):
                continue
            deeper = _descend(value, line, trail + [(str(key.value), value)])
            if len(deeper) > len(best):
                best = deeper
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            deeper = _descend(item, line, trail + [(f"[{index}]", item)])
            if len(deeper) > len(best):
                best = deeper
    return best


def _is_an_object_id(word: str) -> bool:
    """Whether a key could be the object part of an entity id."""
    return bool(word) and not word.startswith("[") and all(one.isalnum() or one == "_" for one in word)


def _name_the_holder(steps: List[Tuple[str, object]]) -> Where:
    """What to call the thing a line sits in, from the path down to it.

    Two shapes carry a name. A mapping of object ids under a domain key - what
    `script:` and the helper domains use - names both an id and, inside the
    block, an alias. A list of objects - what `automation:` and `scene:` use,
    and what a file of automations is at its root - names no id, so the alias
    inside the entry is all there is.

    Anything else keeps the path and no name, which still says more than a line
    number on its own.
    """
    trail = [step for step, _node in steps]
    if not steps:
        return Where(trail)

    # The outermost block that states a name of its own, and the id if the
    # structure around it gives one.
    name = None
    object_id = None
    for depth, (step, node) in enumerate(steps):
        found = _scalar_under(node, NAME_FIELDS)
        if found is None:
            continue
        name = found
        if depth >= 1:
            above = steps[depth - 1][0]
            if _is_an_object_id(step) and _is_an_object_id(above) and depth == 1:
                object_id = f"{above}.{step}"
        break

    if name is None:
        # A block with no name of its own may still be keyed by an object id.
        if len(steps) >= 2 and _is_an_object_id(steps[0][0]) and _is_an_object_id(steps[1][0]):
            object_id = f"{steps[0][0]}.{steps[1][0]}"
        elif len(steps) >= 1:
            name = _scalar_under(steps[0][1], ID_FIELDS)

    return Where(trail, name, object_id)


class Structures:
    """Reads files for what holds a line, parsing each one at most once."""

    def __init__(self, root: Optional[str]):
        self.root = root
        self._trees: Dict[str, Optional[object]] = {}

    def forget(self) -> None:
        self._trees = {}

    def _tree(self, relative: str):
        if relative in self._trees:
            return self._trees[relative]
        tree = None
        full = os.path.join(self.root or "", relative)
        try:
            if os.path.getsize(full) <= _TOO_LARGE_TO_PARSE:
                with open(full, "r", encoding="utf-8", errors="replace") as handle:
                    tree = yaml.compose(handle, Loader=Forgiving)
        except (OSError, yaml.YAMLError, RecursionError) as error:  # noqa: BLE001 - the line still stands
            logger.debug("Could not read the structure of %s: %s", relative, error)
            tree = None
        self._trees[relative] = tree
        return tree

    def what_holds(self, relative: str, line: int) -> Optional[Where]:
        """The object a line sits in, or None when the file cannot be read."""
        tree = self._tree(relative)
        if tree is None:
            return None
        steps = _descend(tree, line, [])
        if not steps:
            return None
        return _name_the_holder(steps)
