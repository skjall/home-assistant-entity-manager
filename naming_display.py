"""Spelling of a type name as it is shown, independent of any rule.

Integrations spell the same type in many ways: "Firmware", "firmware",
"FIRMWARE", "nacht_rot". Users want one spelling without writing a rule for
each variant. Only the spelling is touched here, never the wording; a
different word is what a rule is for.
"""

import re
from typing import Dict

# Casing modes the user can pick in the settings.
CASE_OFF = "off"
CASE_FIRST_WORD = "first_word"  # first word capitalised, the rest kept as supplied
CASE_SENTENCE = "sentence"  # first word capitalised, the rest lower-cased
CASE_MODES = (CASE_OFF, CASE_FIRST_WORD, CASE_SENTENCE)
DEFAULT_CASE = CASE_FIRST_WORD

# Tokens that keep a fixed spelling in every mode, keyed by their upper-case form.
_PROTECTED: Dict[str, str] = {
    token.upper(): token
    for token in (
        "LED",
        "RGB",
        "RGBW",
        "RGBWW",
        "WLAN",
        "WiFi",
        "Wi-Fi",
        "LAN",
        "CO",
        "CO2",
        "VOC",
        "TVOC",
        "PM1",
        "PM2.5",
        "PM10",
        "UV",
        "IP",
        "IPv4",
        "IPv6",
        "MAC",
        "SSID",
        "USB",
        "HDMI",
        "IR",
        "RF",
        "PIR",
        "CPU",
        "RAM",
        "AC",
        "DC",
        "PV",
        "HVAC",
        "DNS",
        "NTP",
        "API",
        "MQTT",
        "BLE",
        "GPS",
        "LTE",
        "LQI",
        "RSSI",
        "SNR",
        "PWM",
        "EV",
        "OTA",
        "ID",
        "URL",
        "DHCP",
        "VPN",
        "TV",
        "AV",
        "HDR",
        "DSP",
        "USV",
        "UPS",
        "HTTP",
        "HTTPS",
        "DMZ",
        "BSSID",
        "PPFD",
        "HCHO",
        "VLAN",
        "WAN",
        "NAS",
        "SSD",
        "HDD",
        "PoE",
        "IoT",
        "SoC",
    )
}
_SLUG = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)+$")
_TRIM = ".,;:()[]"


def _protected(token: str) -> str:
    """Return the fixed spelling for ``token`` (punctuation kept), or "".

    Only an all-lower or all-upper token is rewritten ("led", "LED"); a
    mixed spelling such as "Mac" was chosen on purpose and stays.
    """
    core = token.strip(_TRIM)
    fixed = _PROTECTED.get(core.upper())
    if fixed is None or core == fixed or not (core.islower() or core.isupper()):
        return ""
    return token.replace(core, fixed, 1)


def _shouting(name: str) -> bool:
    """A whole name in capitals ("FIRMWARE", "POWER-ON BEHAVIOR") is shouting, not an acronym."""
    letters = "".join(char for char in name if char.isalpha())
    return len(letters) >= 6 and letters.isupper() and not _PROTECTED.get(name.strip(_TRIM).upper())


def _keep_as_is(core: str) -> bool:
    # Digits inside a token (CO2, 5GHz, °C), several capitals (ZigBee, RGBW)
    # or a capital after a lower-case start (iOS, eBay) mark a spelling the
    # integration chose deliberately.
    if any(char.isdigit() for char in core):
        return True
    if sum(1 for char in core if char.isupper()) >= 2:
        return True
    return bool(core) and core[0].islower() and core[1:] != core[1:].lower()


def _case_token(token: str, first: bool, mode: str, shouting: bool = False) -> str:
    fixed = _protected(token)
    if fixed:
        return fixed
    core = token.strip(_TRIM)
    if not core or (_keep_as_is(core) and not shouting):
        return token
    if mode == CASE_SENTENCE or shouting:
        parts = token.lower().split("-")
        if first:
            parts[0] = parts[0][:1].upper() + parts[0][1:]
        return "-".join(parts)
    if first:
        return token[:1].upper() + token[1:]
    return token


def normalize_display(name: str, mode: str = DEFAULT_CASE) -> str:
    """Return ``name`` in the chosen spelling; idempotent, wording untouched."""
    if not name or not name.strip():
        return name
    if _SLUG.match(name):
        # Object-id style names ("nacht_rot") carry no casing to keep; every
        # word is capitalised, which is how such scenes were named before
        # the integration turned them into slugs.
        return " ".join(part.capitalize() for part in name.split("_"))
    if mode == CASE_OFF:
        return name
    tokens = name.split()
    shouting = _shouting(name)
    return " ".join(_case_token(token, index == 0, mode, shouting) for index, token in enumerate(tokens))
