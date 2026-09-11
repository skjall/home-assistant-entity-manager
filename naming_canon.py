"""Canonical form for type-rule keys.

A rule has to match the name an integration supplies no matter how that name
is spelled: "Effect speed", "effect_speed" and "Effect-Speed" are one type.
Both learning a rule and looking one up go through ``canon``, so the two
sides can never disagree on spelling.
"""

import re
import unicodedata

_GERMAN = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "ae", "Ö": "oe", "Ü": "ue"})
_NON_KEY = re.compile(r"[^a-z0-9]+")


def canon(value: str) -> str:
    """Return the canonical key for ``value``, or "" when nothing is left."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).translate(_GERMAN)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return _NON_KEY.sub("_", text.lower()).strip("_")
