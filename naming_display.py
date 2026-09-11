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
    )
}
_SLUG = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)+$")
_TRIM = ".,;:()[]"


def _protected(token: str) -> str:
    """Return the fixed spelling for ``token`` (punctuation kept), or ""."""
    core = token.strip(_TRIM)
    fixed = _PROTECTED.get(core.upper())
    if fixed is None:
        return ""
    return token.replace(core, fixed, 1)


def _shouting(core: str) -> bool:
    """All capitals over four or more letters is shouting, not an acronym."""
    letters = core.replace("-", "")
    return len(letters) >= 4 and letters.isalpha() and letters.isupper()


def _keep_as_is(core: str) -> bool:
    # Digits inside a token (CO2, 5GHz, °C), several capitals (ZigBee, RGBW)
    # or a capital after a lower-case start (iOS, eBay) mark a spelling the
    # integration chose deliberately.
    if any(char.isdigit() for char in core):
        return True
    if _shouting(core):
        return False
    if sum(1 for char in core if char.isupper()) >= 2:
        return True
    return bool(core) and core[0].islower() and core[1:] != core[1:].lower()


def _case_token(token: str, first: bool, mode: str) -> str:
    fixed = _protected(token)
    if fixed:
        return fixed
    core = token.strip(_TRIM)
    if not core or _keep_as_is(core):
        return token
    if mode == CASE_SENTENCE or _shouting(core):
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
    return " ".join(_case_token(token, index == 0, mode) for index, token in enumerate(tokens))
