#!/usr/bin/env python3
"""
Entity Reference Utilities - Zentrale, wortgrenzen-sichere Ersetzung von Entity-IDs.

Wird von dependency_updater.py, dependency_scanner.py und lovelace_updater.py
gemeinsam genutzt, damit die Ersetzungslogik nur an einer Stelle lebt.

Wichtig fuer den Geraete-Austausch: alte und neue entity_id haben voellig
unterschiedliche slugs (z.B. binary_sensor.kuche_fenster_tur ->
binary_sensor.kuche_fenster_neu_zustand). Eine reine Substring-Ersetzung wuerde
z.B. `..._tur` faelschlich in `..._tur_2` treffen. Daher:

- Direkte Werte / Listenelemente: nur bei EXAKTER Gleichheit ersetzen.
- Template-Strings ({{ ... }}): per Regex mit Wortgrenzen (\\b) ersetzen.
- Normale Freitext-Strings (kein Template): unangetastet lassen.
"""

import re
from typing import Any, Tuple

_ENTITY_ID_RE = re.compile(r"\b([a-z_]+\.[a-z0-9_]+)\b")


def extract_entity_ids(data: Any) -> set:
    """Sammelt rekursiv alle Strings, die wie eine entity_id (domain.object_id) aussehen.

    Bewusst grob (auch in Freitext/Templates) - dient nur dem in-use-Check (kommt eine
    konkrete entity_id irgendwo vor?), nicht der exakten Referenz-Klassifizierung.
    """
    found: set = set()

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            for m in _ENTITY_ID_RE.findall(node):
                found.add(m)
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(data)
    return found


def refers_to_entity_in_string(value: str, entity_id: str) -> bool:
    """Whether one string refers to the entity, as a reference and not as prose.

    The one place that decides it: the replacement below asks this rather than
    deciding again, and a read-only caller asks it too instead of imitating a
    replacement with a sentinel value. A reference category added here is
    therefore added to both at once.
    """
    # The value itself, as in entity_id: "light.kueche".
    if value == entity_id:
        return True

    # Not there at all, which is most strings: asked as a substring first, so
    # the pattern is only run where there is something for it to find.
    if entity_id not in value:
        return False

    # A template can name the entity inside an expression. Word boundaries, so
    # that `sensor.temp` does not match `sensor.temperature`.
    return _is_template(value) and re.search(_word_bounded(entity_id), value) is not None


def _is_template(value: str) -> bool:
    """Whether the string is a template, which may name an entity inside it.

    `{% ... %}` counts too: helpers built in the interface often take their
    values through `{% set %}`.
    """
    return ("{{" in value and "}}" in value) or ("{%" in value and "%}" in value)


def _word_bounded(entity_id: str) -> str:
    """The entity id as a pattern that does not match a longer id."""
    return r"\b" + re.escape(entity_id) + r"\b"


def replace_entity_ref_in_string(value: str, old_entity_id: str, new_entity_id: str) -> Tuple[str, bool]:
    """Ersetzt eine Entity-ID in einem einzelnen String.

    Args:
        value: Der zu pruefende String.
        old_entity_id: Die zu ersetzende Entity-ID.
        new_entity_id: Die neue Entity-ID.

    Returns:
        Tupel (neuer_string, wurde_geaendert).
    """
    if value == old_entity_id:
        return new_entity_id, True

    # The same two categories the read-only check knows - the value itself, and
    # a template naming the entity inside an expression - but asked once: the
    # pattern that would find the reference is the pattern that replaces it, and
    # asking first and replacing afterwards ran it twice over every template a
    # rename touches. A new id equal to the old one is no change to make.
    if old_entity_id not in value or not _is_template(value):
        return value, False
    new_value, hits = re.subn(_word_bounded(old_entity_id), new_entity_id, value)
    if not hits or new_value == value:
        return value, False
    return new_value, True


def replace_entity_in_obj(data: Any, old_entity_id: str, new_entity_id: str) -> bool:
    """Ersetzt eine Entity-ID rekursiv in einer beliebigen Datenstruktur (in-place).

    Behandelt Strings (exakt oder Template), Listen und verschachtelte Dicts/Listen.

    Args:
        data: dict, list oder beliebiger Wert (wird in-place mutiert).
        old_entity_id: Die zu ersetzende Entity-ID.
        new_entity_id: Die neue Entity-ID.

    Returns:
        True, wenn irgendwo etwas geaendert wurde.
    """
    changed = False

    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, str):
                new_value, did_change = replace_entity_ref_in_string(value, old_entity_id, new_entity_id)
                if did_change:
                    data[key] = new_value
                    changed = True
            elif isinstance(value, (dict, list)):
                if replace_entity_in_obj(value, old_entity_id, new_entity_id):
                    changed = True

    elif isinstance(data, list):
        for i, item in enumerate(data):
            if isinstance(item, str):
                new_value, did_change = replace_entity_ref_in_string(item, old_entity_id, new_entity_id)
                if did_change:
                    data[i] = new_value
                    changed = True
            elif isinstance(item, (dict, list)):
                if replace_entity_in_obj(item, old_entity_id, new_entity_id):
                    changed = True

    return changed


def refers_to_entity(data: Any, entity_id: str) -> bool:
    """Whether the structure refers to the entity, by the rules of a rename.

    The same reading as ``replace_entity_in_obj``, out of the same function, and
    without writing anything or copying: a value that is the entity id, or a
    template that names it between word boundaries. Prose that happens to
    contain the id - an automation described as "watches sensor.old" - is not a
    reference, and a rename that correctly leaves it alone must not be reported
    as one that failed.

    Values, not keys: a key is not rewritten either, so reporting one would be
    reporting a reference nothing here can carry over.
    """
    if isinstance(data, str):
        return refers_to_entity_in_string(data, entity_id)
    if isinstance(data, dict):
        return any(refers_to_entity(value, entity_id) for value in data.values())
    if isinstance(data, list):
        return any(refers_to_entity(item, entity_id) for item in data)
    return False
