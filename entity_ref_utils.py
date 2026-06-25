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


def replace_entity_ref_in_string(value: str, old_entity_id: str, new_entity_id: str) -> Tuple[str, bool]:
    """Ersetzt eine Entity-ID in einem einzelnen String.

    Args:
        value: Der zu pruefende String.
        old_entity_id: Die zu ersetzende Entity-ID.
        new_entity_id: Die neue Entity-ID.

    Returns:
        Tupel (neuer_string, wurde_geaendert).
    """
    # Exakter Wert (z.B. entity_id: "light.kueche")
    if value == old_entity_id:
        return new_entity_id, True

    # Templates: Entity-ID kann als Teil eines Jinja-Ausdrucks vorkommen.
    # Nur mit Wortgrenzen ersetzen, damit `sensor.temp` nicht in
    # `sensor.temperature` trifft.
    if "{{" in value and "}}" in value and old_entity_id in value:
        pattern = r"\b" + re.escape(old_entity_id) + r"\b"
        new_value = re.sub(pattern, new_entity_id, value)
        if new_value != value:
            return new_value, True

    return value, False


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
