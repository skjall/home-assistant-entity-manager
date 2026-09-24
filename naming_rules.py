"""Type rules: what the entity-specific part of a name is called.

A rule maps an entity's type — identified by its integration's
``translation_key``, the canonical form of the name the integration supplies,
or its ``device_class`` — to the wording the user wants, per language.

How far a rule reaches is a list of filters rather than one fixed scope. A
filter names an integration, an integration and a device model, or a single
entity by its registry id. A rule with no filter at all reaches every entity of
its type. Several filters on one rule are read as "or", so one rule can say
"this wording, for ecoflow_cloud and for matter" without being written twice.

Where more than one rule could answer, the narrowest filter decides: one
entity, then a model, then an integration, then everywhere. Two rules may
therefore never claim the same filter for the same type — that is refused on
write rather than resolved by guessing.

Storage: ``/data/naming_rules.json``::

    {"version": 2, "language": "de",
     "rules": [{"id": "r_ab12cd34",
                "match": {"kind": "name", "value": "linkquality"},
                "filters": [{"integration": "mqtt"}],
                "targets": {"de": "Verbindungsqualität"},
                "source": "learned", "learned_from": "sensor.x", "created_at": "..."}],
     "migration": {...}}

A pattern rule matches the supplied name against a regular expression instead
of its exact wording. Integrations that put a serial number into every name -
"Heating 12345678", "Heating 12345679" - supply as many names as they have
devices, and an exact rule reaches only one of them. The pattern leaves the
number open, and the target can carry it on: ``{1}`` stands for the first
number, ``{name}`` for a named group of a hand-written expression. A pattern
always applies within one integration: a name is only a pattern of what one
integration writes, and "any name with a number in it" is no type at all.

The legacy store ``user_type_mappings.json`` is migrated on first load; its
backup and a report stay next to it so nothing is lost silently. Rules written
before the filter list are migrated in place: their single scope becomes their
one filter.
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import shutil
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
import uuid

from json_store import atomically, guarded, new_lock
from naming_canon import canon
from naming_display import CASE_MODES, DEFAULT_CASE, normalize_display

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
KINDS = ("translation_key", "name", "pattern", "device_class")
# Lookup order: the most specific identity first. A pattern is less specific
# than the exact name it would also match, so a rule for that name wins.
KIND_PRIORITY = {"translation_key": 0, "name": 1, "pattern": 2, "device_class": 3}

# Kinds whose value is kept as written rather than in canonical form: a
# translation key is an identifier, and a pattern is matched against the name
# as supplied, spaces and all.
VERBATIM_KINDS = ("translation_key", "pattern")

# A run of digits is what a pattern learned from one name leaves open.
_NUMBER = re.compile(r"\d+")
_OPEN_NUMBER = re.compile(r"\(\?P<n(\d+)>\\d\+\)")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
# Names are short; a longer expression is a mistake, not a pattern.
MAX_PATTERN_LENGTH = 500

# What a filter may say. A registry id names one entity and stands alone; the
# others describe a kind of entity and may be combined.
#
# The domain is there because an integration supplies one name for what it
# measures and what it sets: ecoflow_cloud calls both "Custom Load Power", one a
# sensor and one a number. Without it the two cannot be told apart by a rule,
# only one entity at a time.
FILTER_FIELDS = ("registry_id", "integration", "model", "domain")

# Narrowest first. Nothing at all is widest and comes last.
EVERYWHERE_RANK = 8


class NamingRuleError(ValueError):
    """Raised for an invalid rule."""


def rule_key(kind: str, value: str) -> str:
    """The form a rule of this kind stores and looks up its value in."""
    return value if kind in VERBATIM_KINDS else canon(value)


def pattern_of(example: str) -> Optional[Tuple[str, List[str]]]:
    """The pattern a supplied name makes with its numbers left open, and the numbers.

    None where the name carries no number: without one there is nothing to
    leave open, and the exact rule already says everything.
    """
    numbers = _NUMBER.findall(example or "")
    if not numbers:
        return None
    parts = _NUMBER.split(example)
    regex = "".join(
        re.escape(part) + (rf"(?P<n{index + 1}>\d+)" if index < len(numbers) else "")
        for index, part in enumerate(parts)
    )
    return regex, numbers


def readable_pattern(regex: str) -> str:
    """A pattern as a person reads it: "Heating {1}" rather than its expression.

    Only a pattern learned from a name reads that way; one written by hand is
    shown as written, since anything else would claim more than it knows.
    """
    pieces, position = [], 0
    for match in _OPEN_NUMBER.finditer(regex):
        pieces.append(re.sub(r"\\(.)", r"\1", regex[position : match.start()]))
        pieces.append("{" + match.group(1) + "}")
        position = match.end()
    pieces.append(re.sub(r"\\(.)", r"\1", regex[position:]))
    text = "".join(pieces)
    rebuilt = "".join(
        re.escape(part) if index % 2 == 0 else rf"(?P<n{part}>\d+)"
        for index, part in enumerate(re.split(r"\{(\d+)\}", text))
    )
    return text if rebuilt == regex else regex


def target_of(typed: str, numbers: List[str]) -> str:
    """The target a typed name makes: each number of the example becomes its placeholder.

    Typing "Heat cost allocator 12345678" for "Heating 12345678" means the
    number stays whatever it is on the next device. A number the typed name
    does not repeat is simply not carried.
    """
    # One at a time, and each number only where it has not already been
    # replaced: "Zone 10 Panel 10" holds the same number twice, and replacing
    # every occurrence at once gave both of them the first placeholder.
    target = typed
    taken: List[Tuple[int, int]] = []
    for index, number in sorted(enumerate(numbers, 1), key=lambda pair: -len(pair[1])):
        for found in re.finditer(rf"(?<![\d{{]){re.escape(number)}(?![\d}}])", target):
            if any(start < found.end() and found.start() < end for start, end in taken):
                continue
            placeholder = "{" + str(index) + "}"
            target = target[: found.start()] + placeholder + target[found.end() :]
            shift = len(placeholder) - (found.end() - found.start())
            taken = [(start + shift, end + shift) if start > found.start() else (start, end) for start, end in taken]
            taken.append((found.start(), found.start() + len(placeholder)))
            break
    return target


# The syntax that opens a group: a plain "(", or one of the extensions whose
# question mark says what kind of group it is rather than repeating anything.
_GROUP_OPEN = re.compile(r"\(\?(?:P<\w+>|P=\w+|<[=!]|[:=!>#])")

# A counted quantifier: "{4}", "{1,3}", or the open-ended "{2,}".
_COUNT = re.compile(r"\{\d+(?P<open>,(?!\d))?(?:,\d+)?\}")


def _refuse_runaway(regex: str) -> None:
    """Refuse a quantifier that is applied to something that already repeats.

    "(a+)+" and its kin take exponentially long on a name that nearly matches,
    and every entity of the integration is matched against the pattern on every
    resolution - one such expression would stop the add-on answering at all.
    Nothing this add-on writes has that shape: a learned pattern quantifies the
    digits it left open and nothing else.
    """
    quantifiers = {"*", "+", "?", "{"}
    repeats = [False]  # whether the group at each depth already repeats
    at = 0
    in_class = False
    while at < len(regex):
        char = regex[at]
        if char == "\\":
            at += 2
            continue
        opening = _GROUP_OPEN.match(regex, at)
        if opening and not in_class:
            # "(?P<n1>" and its kin open a group; the question mark in them is
            # not a quantifier and must not be read as one.
            repeats.append(False)
            at = opening.end()
            continue
        if in_class:
            # Inside [...] a star is a star: "([a+])+" repeats a class of two
            # characters, which finishes in time like any other.
            in_class = char != "]"
            at += 1
            continue
        if char == "[":
            in_class = True
        elif char == "(":
            repeats.append(False)
        elif char == ")":
            inside = repeats.pop() if len(repeats) > 1 else False
            after = regex[at + 1 : at + 2]
            # A bounded count after the group is no more a runaway than the
            # group itself; an open-ended one is.
            if after == "{":
                counted = _COUNT.match(regex, at + 1)
                if counted and not counted.group("open"):
                    after = ""
            if inside and after in quantifiers and after != "?":
                raise NamingRuleError("A pattern may not repeat what already repeats: it would never finish")
            # "?" and "*" as well: "((ab)?)+" repeats a group that can match
            # nothing, and the inner group alone said nothing about that.
            if inside or after in {"*", "+", "{", "?"}:
                repeats[-1] = True
        elif char == "{":
            counted = _COUNT.match(regex, at)
            if counted:
                # "{4}" and "{1,3}" bound what they repeat, so what they
                # repeat finishes: "(\\d{4})+" is not the shape that runs
                # away. An open end - "{2,}" - is, and reads as one below.
                if counted.group("open"):
                    repeats[-1] = True
                at = counted.end()
                continue
            repeats[-1] = True
        elif char in {"*", "+", "?"}:
            # A question mark counts: "(Sensor ?)+" repeats a group that can
            # match nothing, which backtracks just as badly as "(a+)+". A
            # question mark on a group of its own is read at the ")" above and
            # is fine - "(ab)?c" finishes in time.
            repeats[-1] = True
        at += 1


def compile_pattern(regex: str) -> "re.Pattern[str]":
    """A pattern's expression, compiled, or a NamingRuleError saying why not."""
    if not regex or len(regex) > MAX_PATTERN_LENGTH:
        raise NamingRuleError("A pattern needs an expression of at most 500 characters")
    _refuse_runaway(regex)
    try:
        return re.compile(regex)
    except re.error as error:
        raise NamingRuleError(f"Not a valid pattern: {error}") from error


def fill_placeholders(target: str, match: "re.Match[str]") -> Optional[str]:
    """The target with every placeholder replaced by what the name had there.

    None where a placeholder has nothing to put there: an expression like
    "Zone (?P<n1>\\d*)" matches "Zone" with no number at all, and the target
    then came out as the text around a hole. The rule does not apply to such a
    name rather than renaming it to half of what it says.
    """
    missing = False

    def value(found: "re.Match[str]") -> str:
        nonlocal missing
        got = _group(match, found.group(1))
        if not got:
            missing = True
            return ""
        return got

    filled = _PLACEHOLDER.sub(value, target)
    return None if missing else filled


def _group(match: "re.Match[str]", key: str) -> Optional[str]:
    """A placeholder's group: by name, as a learned number, or by position."""
    groups = match.re.groupindex
    if key in groups:
        return match.group(key)
    if f"n{key}" in groups:
        return match.group(f"n{key}")
    if key.isdigit() and 0 < int(key) <= match.re.groups:
        return match.group(int(key))
    return None


def _known_placeholder(pattern: "re.Pattern[str]", key: str) -> bool:
    return (
        key in pattern.groupindex
        or f"n{key}" in pattern.groupindex
        or (key.isdigit() and 0 < int(key) <= pattern.groups)
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return f"r_{uuid.uuid4().hex[:8]}"


def clean_filter(raw: Any) -> Dict[str, str]:
    """One filter, reduced to the fields that say something."""
    if not isinstance(raw, Mapping):
        raise NamingRuleError("A filter has to be an object")
    unknown = set(raw) - set(FILTER_FIELDS)
    if unknown:
        raise NamingRuleError(f"A filter knows no {', '.join(sorted(unknown))}")
    registry_id = str(raw.get("registry_id") or "").strip()
    integration = str(raw.get("integration") or "").strip()
    model = str(raw.get("model") or "").strip()
    # A domain is written one way by Home Assistant and there is nothing to
    # match loosely: `Sensor` and `sensor` are the same domain.
    domain = str(raw.get("domain") or "").strip().lower()
    if registry_id:
        if integration or model or domain:
            raise NamingRuleError("A filter names one entity or a kind of device, not both")
        return {"registry_id": registry_id}
    cleaned = {}
    if integration:
        cleaned["integration"] = integration
    if model:
        cleaned["model"] = model
    if domain:
        cleaned["domain"] = domain
    if not cleaned:
        raise NamingRuleError("A filter that says nothing is the rule without filters")
    return cleaned


def filter_rank(one: Mapping[str, str]) -> int:
    """How narrow a filter is: 0 is one entity, 8 is everything.

    A device is narrower than an integration, and naming a domain narrows
    whichever of those a filter already says - "sensors of this model" reaches
    fewer entities than "this model".
    """
    if one.get("registry_id"):
        return 0
    integration = one.get("integration")
    model = one.get("model")
    domain = one.get("domain")
    if integration and model:
        return 1 if domain else 2
    if model:
        return 3 if domain else 4
    if integration:
        return 5 if domain else 6
    if domain:
        return 7
    return EVERYWHERE_RANK


def clean_filters(raw: Any) -> List[Dict[str, str]]:
    """A rule's filters, without repeats and in a settled order.

    The same filter twice reaches no further than once, so it is dropped rather
    than kept and explained later. Sorting them makes two rules with the same
    reach compare equal whatever order they were typed in.
    """
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise NamingRuleError("Filters have to be a list")
    seen: List[Dict[str, str]] = []
    for entry in raw:
        one = clean_filter(entry)
        if one not in seen:
            seen.append(one)
    return sorted(seen, key=lambda one: (filter_rank(one), sorted(one.items())))


class NamingRules:
    """Validate, resolve and persist type rules."""

    def __init__(
        self,
        storage_path: str = "/data/naming_rules.json",
        legacy_path: Optional[str] = None,
        device_class_keys: Iterable[str] = (),
        default_language: str = "en",
    ) -> None:
        # One lock per store: a read-change-write stays one step.
        self._lock = new_lock()
        self.storage_path = Path(storage_path)
        self.legacy_path = Path(legacy_path) if legacy_path else None
        self.device_class_keys = {canon(key) for key in device_class_keys}
        self.default_language = default_language
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._rule_index = None
        self._by_entity = None
        self._patterns: Optional[List[Tuple[Dict[str, Any], "re.Pattern[str]"]]] = None
        self.data = self._load()

    # ------------------------------------------------------------------ storage

    def _default_data(self) -> Dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "language": self.default_language,
            "display_case": DEFAULT_CASE,
            "pattern_rules": False,
            "rules": [],
            "migration": None,
        }

    def _load(self) -> Dict[str, Any]:
        if self.storage_path.exists():
            try:
                with self.storage_path.open("r", encoding="utf-8") as file:
                    data = json.load(file)
                if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
                    raise NamingRuleError("Stored rules must be an object with a rules list")
                data.setdefault("version", SCHEMA_VERSION)
                data.setdefault("language", self.default_language)
                data.setdefault("migration", None)
                data.setdefault("display_case", DEFAULT_CASE)
                data.setdefault("pattern_rules", False)
                self._migrate_scopes_to_filters(data)
                return data
            except (OSError, json.JSONDecodeError, NamingRuleError) as error:
                logger.error("Failed to load naming rules: %s", error)
                return self._default_data()

        data = self._default_data()
        if self.legacy_path and self.legacy_path.exists():
            self._migrate_legacy(data)
            self._write(data)
        return data

    @staticmethod
    def _migrate_scopes_to_filters(data: Dict[str, Any]) -> None:
        """Turn the one scope a rule used to carry into its one filter.

        Runs on every load rather than once behind a flag: it is idempotent, and
        a rule written by an older version can still arrive through an import or
        a restored backup long after the file itself says version 2.
        """
        for rule in data.get("rules", []):
            match = rule.setdefault("match", {})
            if "filters" not in rule:
                scope = {}
                if match.get("integration"):
                    scope["integration"] = match["integration"]
                if match.get("model"):
                    scope["model"] = match["model"]
                rule["filters"] = [scope] if scope else []
            else:
                rule["filters"] = clean_filters(rule["filters"])
            match.pop("integration", None)
            match.pop("model", None)
        data["version"] = SCHEMA_VERSION

    def _write(self, data: Mapping[str, Any]) -> None:
        atomically(self.storage_path, data)

    def _forget_index(self) -> None:
        self._rule_index = None
        self._by_entity = None
        self._patterns = None

    @guarded
    def save(self) -> None:
        self._forget_index()
        self._write(self.data)

    # ---------------------------------------------------------------- migration

    def _migrate_legacy(self, data: Dict[str, Any]) -> None:
        """Turn ``{"user_mappings": {key: value}}`` into rules, with a report."""
        try:
            with self.legacy_path.open("r", encoding="utf-8") as file:
                legacy = json.load(file)
            mappings = legacy.get("user_mappings", {}) if isinstance(legacy, dict) else {}
        except (OSError, json.JSONDecodeError) as error:
            logger.error("Failed to read legacy type mappings: %s", error)
            return

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_dir = self.storage_path.parent / "migrations" / stamp
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.legacy_path, backup_dir / self.legacy_path.name)

        language = data["language"]
        grouped: Dict[tuple, Dict[str, Any]] = {}
        for raw_key, value in mappings.items():
            key = canon(raw_key)
            if not key or not isinstance(value, str) or not value.strip():
                continue
            kind = "device_class" if key in self.device_class_keys else "name"
            group = grouped.setdefault((kind, key), {"values": {}, "legacy_keys": []})
            group["legacy_keys"].append(raw_key)
            group["values"].setdefault(value.strip(), []).append(raw_key)

        rules: List[Dict[str, Any]] = []
        report = {
            "at": _now(),
            "backup": str(backup_dir),
            "imported": 0,
            "merged": [],
            "conflicts": [],
            "reclassified": [],
            "skipped": [],
        }
        for (kind, key), group in sorted(grouped.items()):
            values = list(group["values"])
            # A mapping that only repeats the original in another spelling
            # ("firmware" -> "Firmware") is what the display spelling does anyway.
            if len(values) == 1 and canon(values[0]) == key:
                report["skipped"].append({"key": key, "value": values[0], "legacy_keys": group["legacy_keys"]})
                continue
            # Several legacy spellings with one value collapse into one rule.
            # Different values for one key are kept as alternatives and reported.
            winner = values[-1]
            rule = self._make_rule(kind, key, None, {language: winner}, source="migrated")
            rule["legacy_keys"] = group["legacy_keys"]
            if len(values) > 1:
                rule["alternatives"] = [value for value in values if value != winner]
                report["conflicts"].append({"rule_id": rule["id"], "key": key, "values": values, "chosen": winner})
            if len(group["legacy_keys"]) > 1:
                report["merged"].append({"rule_id": rule["id"], "key": key, "legacy_keys": group["legacy_keys"]})
            if kind == "device_class":
                report["reclassified"].append({"rule_id": rule["id"], "key": key})
            rules.append(rule)
            report["imported"] += 1

        data["rules"] = rules
        data["migration"] = report
        logger.info(
            "Migrated %d legacy type mappings into %d rules (%d merged, %d conflicts)",
            len(mappings),
            len(rules),
            len(report["merged"]),
            len(report["conflicts"]),
        )

    @guarded
    def repair_kinds(
        self,
        translation_keys: Iterable[str],
        names: Iterable[str],
        device_classes: Iterable[str] = (),
    ) -> Optional[Dict[str, Any]]:
        """Move rules that are stored under the wrong kind, once.

        Older versions filed every rule under the entity's name. A value that is
        in truth a device class or an integration's translation key never equals
        a display name, so such a rule can never match anything.
        """
        if self.data.get("repair"):
            return None
        known_names = {canon(name) for name in names if name}
        known_keys = {key for key in translation_keys if key}
        # A home can use classes the built-in list does not name.
        known_classes = self.device_class_keys | {canon(value) for value in device_classes if value}
        moved = []
        for rule in self.rules:
            match = rule["match"]
            if match["kind"] != "name":
                continue
            value = canon(match["value"])
            if not value or value in known_names:
                continue
            if value in known_classes:
                kind = "device_class"
            elif match["value"] in known_keys or value in known_keys:
                kind = "translation_key"
            else:
                continue
            match["kind"] = kind
            rule["updated_at"] = _now()
            moved.append({"rule_id": rule["id"], "value": match["value"], "kind": kind})

        if moved:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup_dir = self.storage_path.parent / "migrations" / stamp
            backup_dir.mkdir(parents=True, exist_ok=True)
            if self.storage_path.exists():
                shutil.copy2(self.storage_path, backup_dir / self.storage_path.name)
            report = {"at": _now(), "backup": str(backup_dir), "moved": moved}
        else:
            report = {"at": _now(), "backup": None, "moved": []}
        self.data["repair"] = report
        self.save()
        logger.info("Repaired %d rules that were filed under the wrong kind", len(moved))
        return report

    def unused(
        self, counts: Mapping[str, int], language: str = "", builtins: Mapping[str, str] = {}
    ) -> List[Dict[str, Any]]:
        """Rules that change nothing: no entity matches, or the name is the standard."""
        language = language or self.language
        useless = []
        for rule in self.rules:
            if not counts.get(rule["id"]):
                useless.append(rule)
            elif self.is_redundant(rule, language, builtins.get(rule["id"])):
                useless.append(rule)
        return useless

    @guarded
    def delete_many(self, rule_ids: Iterable[str]) -> int:
        """Remove several rules at once, reporting how many went."""
        wanted = set(rule_ids)
        before = len(self.rules)
        self.data["rules"] = [rule for rule in self.rules if rule["id"] not in wanted]
        removed = before - len(self.rules)
        if removed:
            self.save()
        return removed

    # ------------------------------------------------------------------- rules

    @staticmethod
    def _make_rule(
        kind: str,
        value: str,
        integration: Optional[str],
        targets: Mapping[str, str],
        source: str = "user",
        learned_from: Optional[str] = None,
        model: Optional[str] = None,
        domain: Optional[str] = None,
        filters: Any = None,
    ) -> Dict[str, Any]:
        if kind not in KINDS:
            raise NamingRuleError(f"Unknown rule kind: {kind}")
        if not value:
            raise NamingRuleError("A rule needs a match value")
        if kind == "pattern":
            compile_pattern(value)
        clean_targets = {lang: text.strip() for lang, text in targets.items() if isinstance(text, str) and text.strip()}
        if not clean_targets:
            raise NamingRuleError("A rule needs at least one target")
        if filters is None:
            # The older way of saying it: one integration, one model, or neither.
            scope = {}
            if integration:
                scope["integration"] = integration
            if model:
                scope["model"] = model
            if domain:
                scope["domain"] = str(domain).lower()
            filters = [scope] if scope else []
        rule = {
            "id": _new_id(),
            "match": {"kind": kind, "value": value},
            "filters": clean_filters(filters),
            "targets": clean_targets,
            "source": source,
            "created_at": _now(),
        }
        if learned_from:
            rule["learned_from"] = learned_from
        return rule

    @property
    def language(self) -> str:
        return self.data.get("language") or self.default_language

    @property
    def display_case(self) -> str:
        return self.data.get("display_case") or DEFAULT_CASE

    @property
    def pattern_rules(self) -> bool:
        """Whether pattern rules can be written from the UI.

        Only the writing: a pattern rule already stored applies either way,
        or switching the setting off would silently rename entities back.
        """
        return bool(self.data.get("pattern_rules"))

    @guarded
    def set_pattern_rules(self, enabled: bool) -> None:
        self.data["pattern_rules"] = bool(enabled)
        self.save()

    @guarded
    def set_display_case(self, mode: str) -> None:
        if mode not in CASE_MODES:
            raise NamingRuleError(f"Unknown display case: {mode}")
        self.data["display_case"] = mode
        self.save()

    def is_redundant(self, rule: Dict[str, Any], language: str, builtin: Optional[str] = None) -> bool:
        """A rule whose target is what the display spelling or the built-in default yields anyway."""
        target = rule["targets"].get(language)
        if not target or rule["match"]["kind"] == "pattern":
            return False
        if builtin is not None and target == builtin:
            return True
        key = rule["match"]["value"]
        return canon(target) == key and target == normalize_display(key.replace("_", " "), self.display_case)

    @guarded
    def set_language(self, language: str) -> None:
        """Switch the active language.

        Migrated rules carry one target that was recorded under whatever
        language was active at migration time; it belongs to the language the
        user actually works in, so it moves along the first time that is set.
        """
        previous = self.language
        if language != previous:
            for rule in self.rules:
                targets = rule["targets"]
                if rule.get("source") == "migrated" and list(targets) == [previous]:
                    targets[language] = targets.pop(previous)
        self.data["language"] = language
        self.save()

    @property
    def rules(self) -> List[Dict[str, Any]]:
        return self.data["rules"]

    @property
    def migration_report(self) -> Optional[Dict[str, Any]]:
        return self.data.get("migration")

    def get(self, rule_id: str) -> Optional[Dict[str, Any]]:
        return next((rule for rule in self.rules if rule["id"] == rule_id), None)

    @staticmethod
    def _index_key(
        kind: str,
        value: str,
        integration: Optional[str],
        model: Optional[str],
        domain: Optional[str] = None,
    ):
        return (kind, value, integration or None, canon(model or ""), (domain or "").lower() or None)

    @classmethod
    def _keys_of(cls, rule: Mapping[str, Any]) -> List[tuple]:
        """Every place this rule has to be found under.

        One key per filter, because a rule with two filters answers to both. A
        rule without filters gets the one key that stands for everywhere. A
        filter naming one entity is not a type key at all - see _entities_of.
        """
        match = rule["match"]
        filters = [one for one in (rule.get("filters") or []) if not one.get("registry_id")]
        if not filters and not cls._entities_of(rule):
            filters = [{}]
        return [
            cls._index_key(
                match["kind"],
                match["value"],
                one.get("integration"),
                one.get("model"),
                one.get("domain"),
            )
            for one in filters
        ]

    @staticmethod
    def _entities_of(rule: Mapping[str, Any]) -> List[str]:
        """The single entities this rule names, by registry id."""
        return [one["registry_id"] for one in (rule.get("filters") or []) if one.get("registry_id")]

    def _index(self) -> Dict[tuple, Dict[str, Any]]:
        """Rules by what they match on.

        Resolution asks for a rule twice per entity, so a scan over every rule
        would be thousands of comparisons per entity on a large installation.
        """
        if self._rule_index is None:
            index: Dict[tuple, Dict[str, Any]] = {}
            for rule in self.rules:
                for key in self._keys_of(rule):
                    index.setdefault(key, rule)
            self._rule_index = index
        return self._rule_index

    def _entity_index(self) -> Dict[str, Dict[str, Any]]:
        """Rules that name one entity, by that entity's registry id."""
        if self._by_entity is None:
            index: Dict[str, Dict[str, Any]] = {}
            for rule in self.rules:
                for registry_id in self._entities_of(rule):
                    index.setdefault(registry_id, rule)
            self._by_entity = index
        return self._by_entity

    def for_entity(self, registry_id: str, language: str = "") -> Optional[Dict[str, Any]]:
        """The rule written for this one entity, if there is one.

        It answers whatever the integration supplies, because that is what
        being written for one entity means: the user looked at this entity and
        said what it is called. No type rule gets a say where one exists.
        """
        if not registry_id:
            return None
        rule = self._entity_index().get(registry_id)
        if rule is None:
            return None
        return rule if rule["targets"].get(language or self.language) else None

    def claimed_by(self, rule: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """The rule that already answers to one of these filters, if any.

        Two rules claiming one filter for one type would make the answer depend
        on which came first, so a write that would do it is refused instead.
        """
        index = self._index()
        for key in self._keys_of(rule):
            other = index.get(key)
            if other is not None and other["id"] != rule.get("id"):
                return other
        by_entity = self._entity_index()
        for registry_id in self._entities_of(rule):
            other = by_entity.get(registry_id)
            if other is not None and other["id"] != rule.get("id"):
                return other
        return None

    @staticmethod
    def check_pattern(rule: Mapping[str, Any]) -> None:
        """Refuse a pattern rule that could not be applied as meant.

        It has to name the integration it is about, and every placeholder in
        its targets has to be something the expression captures - otherwise
        the name would come out with a hole in it.
        """
        match = rule["match"]
        if match["kind"] != "pattern":
            return
        pattern = compile_pattern(match["value"])
        filters = rule.get("filters") or []
        if not filters or any(not one.get("integration") or one.get("registry_id") for one in filters):
            raise NamingRuleError("A pattern rule applies within an integration")
        for target in rule["targets"].values():
            for key in _PLACEHOLDER.findall(target):
                if not _known_placeholder(pattern, key):
                    raise NamingRuleError(f"The pattern captures nothing for {{{key}}}")

    def _refuse_collision(self, rule: Mapping[str, Any]) -> None:
        self.check_pattern(rule)
        other = self.claimed_by(rule)
        if other is not None:
            raise NamingRuleError(f"Rule {other['id']} already covers one of those filters")

    @staticmethod
    def _scopes(integration: Optional[str], model: Optional[str], domain: Optional[str] = None):
        """Scopes from narrow to wide.

        This model, then this integration, then everywhere - and each of those
        first for this domain alone, since "sensors of this model" is narrower
        than "this model". A rule written before domains existed says nothing
        about one and is found by the second half of each pair.
        """
        places = [(integration, model), (None, model), (integration, None), (None, None)]
        candidates = []
        for place in places:
            if domain:
                candidates.append(place + (domain,))
            candidates.append(place + (None,))
        seen = []
        for scope in candidates:
            if scope not in seen:
                seen.append(scope)
        return seen

    def _matching(
        self,
        kind: str,
        value: str,
        integration: Optional[str],
        model: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        return self._index().get(self._index_key(kind, value, integration, model, domain))

    @staticmethod
    def matching_filter(
        rule: Mapping[str, Any],
        integration: Optional[str] = None,
        model: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> Dict[str, str]:
        """The narrowest filter of this rule that covers such an entity.

        Empty for a rule without filters, which covers everything. Answering
        with the filter rather than the whole list is what lets a reader see
        why this rule applied here and not somewhere else.
        """
        best: Optional[Dict[str, str]] = None
        for one in rule.get("filters") or []:
            if one.get("registry_id"):
                continue
            if one.get("integration") and one["integration"] != (integration or None):
                continue
            if one.get("model") and canon(one["model"]) != canon(model or ""):
                continue
            if one.get("domain") and one["domain"] != (domain or "").lower():
                continue
            if best is None or filter_rank(one) < filter_rank(best):
                best = one
        return dict(best) if best else {}

    @staticmethod
    def sole_integration(rule: Mapping[str, Any]) -> Optional[str]:
        """The one integration this rule is about, if it is about exactly one.

        A rule spanning several has no single integration whose built-in
        wording it could be compared against, so it gets none.
        """
        found = {one["integration"] for one in rule.get("filters") or [] if one.get("integration")}
        return found.pop() if len(found) == 1 else None

    @classmethod
    def why(
        cls,
        rule: Mapping[str, Any],
        integration: Optional[str] = None,
        model: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> Dict[str, Any]:
        """What a rule matched on, for a reader: its type and the filter that caught this entity."""
        one = cls.matching_filter(rule, integration, model, domain)
        match = rule["match"]
        caught = {
            **match,
            "integration": one.get("integration"),
            "model": one.get("model"),
            "domain": one.get("domain"),
            "filters": [dict(each) for each in rule.get("filters") or []],
        }
        if match["kind"] == "pattern":
            caught["label"] = readable_pattern(match["value"])
        return caught

    def find(
        self,
        kind: str,
        value: Optional[str],
        integration: Optional[str],
        language: str,
        model: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return the rule for ``kind``/``value``, the narrowest scope first."""
        if not value:
            return None
        if kind == "pattern":
            return self._find_pattern(value, integration, language, model, domain)
        key = rule_key(kind, value)
        if not key:
            return None
        for scope_integration, scope_model, scope_domain in self._scopes(
            integration or None, model or None, domain or None
        ):
            rule = self._matching(kind, key, scope_integration, scope_model, scope_domain)
            if rule and rule["targets"].get(language):
                return rule
        return None

    def _pattern_rules(self) -> List[Tuple[Dict[str, Any], "re.Pattern[str]"]]:
        """Pattern rules with their compiled expressions; one that no longer compiles is left out."""
        if self._patterns is None:
            compiled = []
            for rule in self.rules:
                if rule["match"]["kind"] != "pattern":
                    continue
                try:
                    compiled.append((rule, compile_pattern(rule["match"]["value"])))
                except NamingRuleError as error:
                    logger.warning("Pattern rule %s is skipped: %s", rule["id"], error)
            self._patterns = compiled
        return self._patterns

    def _find_pattern(
        self,
        name: str,
        integration: Optional[str],
        language: str,
        model: Optional[str],
        domain: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """The pattern rule for a supplied name, or None.

        Patterns cannot be looked up, only tried. The narrowest filter decides
        as it does for every rule; two patterns that match one name at the same
        reach are a contradiction the user has to settle, so neither applies.
        """
        found: List[Tuple[int, Dict[str, Any]]] = []
        for rule, pattern in self._pattern_rules():
            if not rule["targets"].get(language):
                continue
            one = self.matching_filter(rule, integration, model, domain)
            if not one:
                continue
            match = pattern.fullmatch(name)
            # Matching is not enough: a placeholder the name has nothing for
            # leaves the target with a hole in it, and the rule then renamed
            # the entity to its own template, "{1}" and all.
            if not match or fill_placeholders(rule["targets"][language], match) is None:
                continue
            found.append((filter_rank(one), rule))
        if not found:
            return None
        found.sort(key=lambda pair: pair[0])
        if len(found) > 1 and found[0][0] == found[1][0]:
            # All of them, not the first two: a third rule written to settle
            # the tie between the other two joined it without a word.
            tied = [rule["id"] for rank, rule in found if rank == found[0][0]]
            logger.warning(
                "Pattern rules %s all match %r; none applies",
                ", ".join(tied),
                name,
            )
            return None
        return found[0][1]

    def render(self, rule: Mapping[str, Any], name: str, language: str) -> str:
        """What a rule makes of a supplied name: its target, placeholders filled."""
        target = rule["targets"].get(language) or ""
        if rule["match"]["kind"] != "pattern":
            return target
        # Out of what was compiled for the rules, so a home where every entity
        # matches one pattern does not compile it once per entity.
        # Nothing rather than the target: a target that still holds "{1}" is
        # not a name, and it was written to Home Assistant as one wherever the
        # expression could not be read or did not match.
        unfilled = "" if _PLACEHOLDER.search(target) else target
        pattern = self._compiled_pattern(rule)
        if pattern is None:
            return unfilled
        match = pattern.fullmatch(name or "")
        if not match:
            return unfilled
        filled = fill_placeholders(target, match)
        # Nothing rather than the template: the target with its placeholders
        # still in it is not a name, and it was written to Home Assistant as
        # one.
        return filled.strip() if filled is not None else ""

    def _compiled_pattern(self, rule: Mapping[str, Any]) -> Optional["re.Pattern[str]"]:
        """The compiled expression of a stored pattern rule, or of a passing one."""
        for kept, pattern in self._pattern_rules():
            if kept["id"] == rule.get("id"):
                return pattern
        try:
            return compile_pattern(rule["match"]["value"])
        except NamingRuleError:
            return None

    @guarded
    def upsert(
        self,
        kind: str,
        value: str,
        integration: Optional[str],
        language: str,
        target: str,
        source: str = "user",
        learned_from: Optional[str] = None,
        model: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create or update the rule for one match, setting its target for ``language``."""
        key = rule_key(kind, value)
        rule = self._matching(kind, key, integration or None, model or None, domain or None)
        if rule is None:
            rule = self._make_rule(
                kind, key, integration, {language: target}, source, learned_from, model, domain=domain
            )
            self._refuse_collision(rule)
            self.rules.append(rule)
        else:
            if not target.strip():
                raise NamingRuleError("A rule needs a target")
            # Judged as what it would become, and written only if it passes:
            # writing first left a refused target standing in the rule, and the
            # next save of anything put it on disk.
            wanted = {**rule["targets"], language: target.strip()}
            self.check_pattern({**rule, "targets": wanted})
            rule["targets"] = wanted
            rule["updated_at"] = _now()
            if learned_from:
                rule["learned_from"] = learned_from
        self.save()
        return rule

    @guarded
    def saying(self, kind: str, value: str, language: str, target: str) -> Optional[Dict[str, Any]]:
        """The rule that already says exactly this, whatever it applies to.

        What makes a rule one rule is what it says - this type is called that
        word - not where it applies. Where it applies is the filter list, and a
        second place the same wording is wanted belongs in that list rather than
        in a second rule beside it.
        """
        key = rule_key(kind, value)
        wanted = (target or "").strip()
        for rule in self.rules:
            if rule["match"]["kind"] != kind or rule["match"]["value"] != key:
                continue
            if (rule["targets"].get(language) or "").strip() == wanted:
                return rule
        return None

    @guarded
    def add_filter(
        self,
        kind: str,
        value: str,
        language: str,
        target: str,
        one: Optional[Mapping[str, str]],
        source: str = "user",
        learned_from: Optional[str] = None,
    ) -> Dict[str, Any]:
        rule = self.add_filter_quietly(kind, value, language, target, one, source, learned_from)
        self.save()
        return rule

    def add_filter_quietly(
        self,
        kind: str,
        value: str,
        language: str,
        target: str,
        one: Optional[Mapping[str, str]],
        source: str = "user",
        learned_from: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Make this wording apply in one more place.

        The rule that already says it gains a filter; if none does, one is
        written. Carrying no filter is not a place of its own but the default a
        rule starts out with, so the first real filter replaces it.
        """
        if not (target or "").strip():
            raise NamingRuleError("A rule needs a target")
        # No filter at all is the place that is everywhere, which is how a rule
        # widened from one entity to the whole home arrives here.
        wanted = clean_filter(one) if one else None
        rule = self.saying(kind, value, language, target)
        if rule is None:
            rule = self._make_rule(
                kind,
                rule_key(kind, value),
                None,
                {language: target},
                source=source,
                learned_from=learned_from,
                filters=[wanted] if wanted else [],
            )
            reworded = self._reword_in_place(rule, wanted, language, target)
            if reworded is not None:
                self._forget_index()
                return reworded
            self._refuse_collision(rule)
            self.rules.append(rule)
        elif wanted is None:
            if not rule["filters"]:
                return rule
            self._refuse_collision({**rule, "filters": []})
            rule["filters"] = []
            rule["updated_at"] = _now()
        elif rule.get("filters"):
            merged = clean_filters(list(rule["filters"]) + [wanted])
            if merged == rule["filters"]:
                return rule
            self._refuse_collision({**rule, "filters": merged})
            rule["filters"] = merged
            rule["updated_at"] = _now()
        else:
            # Everywhere is where a rule starts, not somewhere it was put: the
            # first filter takes its place instead of being swallowed by it.
            self._refuse_collision({**rule, "filters": [wanted]})
            rule["filters"] = [wanted]
            rule["updated_at"] = _now()
        self._forget_index()
        return rule

    def _reword_in_place(
        self,
        wanting: Mapping[str, Any],
        one: Optional[Dict[str, str]],
        language: str,
        target: str,
    ) -> Optional[Dict[str, Any]]:
        """Say this type differently where a rule already says it, or None.

        A type is called one thing in one place, so choosing another word for a
        place that is already spoken for is not a second rule but a change of
        mind about the one there. Refusing it left the user at "Rule r_9e6e6268
        already covers one of those filters", with nothing to do about it.

        Where the rule that holds the place holds others too, the place is
        lifted out of it rather than reworded with it: the user asked about
        this one, and the rest keep the word they had.
        """
        held = self.claimed_by(wanting)
        if held is None or held["match"] != wanting["match"]:
            return None
        others = [each for each in (held.get("filters") or []) if each != one]
        if not others:
            held["targets"][language] = target
            held["updated_at"] = _now()
            return held
        # It said this word in more places than the one asked about, so that
        # one leaves and takes the new word with it.
        held["filters"] = others
        held["updated_at"] = _now()
        self.rules.append(dict(wanting))
        return self.rules[-1]

    @guarded
    def merge_duplicates(self) -> List[Dict[str, Any]]:
        """Fold rules that say the same thing into one, filters and all.

        Two rules saying one type is called one word are one rule that applies
        in two places. Kept apart they have to be edited twice and can drift,
        and a reader cannot see from either how far the wording actually
        reaches. One that carries no filter already applies everywhere, so the
        others add nothing and go.
        """
        language = self.language
        first: Dict[tuple, Dict[str, Any]] = {}
        merged: List[Dict[str, Any]] = []
        dropped = set()
        for rule in self.rules:
            key = (rule["match"]["kind"], rule["match"]["value"], (rule["targets"].get(language) or "").strip())
            if not key[2]:
                continue
            kept = first.get(key)
            if kept is None:
                first[key] = rule
                continue
            dropped.add(rule["id"])
            if not kept.get("filters") or not rule.get("filters"):
                # One of them reaches everything of its type; the other is
                # already covered by it.
                kept["filters"] = []
            else:
                kept["filters"] = clean_filters(list(kept["filters"]) + list(rule["filters"]))
            kept["updated_at"] = _now()
            if kept not in merged:
                merged.append(kept)
        if dropped:
            self.data["rules"] = [rule for rule in self.rules if rule["id"] not in dropped]
            self.save()
            logger.info("Folded %d rules into %d that already said the same", len(dropped), len(merged))
        return merged

    @guarded
    def remove_filter(self, rule_id: str, one: Mapping[str, str]) -> Dict[str, Any]:
        """Stop this rule applying in one place.

        Taking the last filter off would widen the rule to everything of its
        type, which is never what removing a place means, so the rule goes
        instead.
        """
        rule = self.get(rule_id)
        if rule is None:
            raise NamingRuleError(f"Unknown rule: {rule_id}")
        wanted = clean_filter(one)
        left = [each for each in (rule.get("filters") or []) if each != wanted]
        if len(left) == len(rule.get("filters") or []):
            raise NamingRuleError("That rule does not apply there")
        if not left:
            self.data["rules"] = [each for each in self.rules if each["id"] != rule_id]
            self.save()
            return {**rule, "deleted": True}
        rule["filters"] = clean_filters(left)
        rule["updated_at"] = _now()
        self.save()
        return rule

    @guarded
    def update(
        self,
        rule_id: str,
        targets: Optional[Mapping[str, str]] = None,
        filters: Any = ...,
    ) -> Dict[str, Any]:
        """Change what a rule says, or the whole list of places it applies.

        One place at a time is add_filter and remove_filter; this is for
        replacing the list wholesale, which is the only other honest way to
        change it. There is deliberately no way to set a single scope: on a
        rule reaching three places that could only mean throwing two away.
        """
        rule = self.get(rule_id)
        if rule is None:
            raise NamingRuleError(f"Unknown rule: {rule_id}")
        if targets is not None:
            clean = {lang: text.strip() for lang, text in targets.items() if isinstance(text, str) and text.strip()}
            if not clean:
                raise NamingRuleError("A rule needs at least one target")
            rule["targets"] = clean
            self.check_pattern(rule)

        if filters is not ...:
            wanted = clean_filters(filters)
            self._refuse_collision({**rule, "filters": wanted})
            rule["filters"] = wanted
        rule["updated_at"] = _now()
        self.save()
        return rule

    @guarded
    def delete(self, rule_id: str) -> bool:
        before = len(self.rules)
        self.data["rules"] = [rule for rule in self.rules if rule["id"] != rule_id]
        if len(self.rules) == before:
            return False
        self.save()
        return True

    @guarded
    def choose_alternative(self, rule_id: str, value: str, language: Optional[str] = None) -> Dict[str, Any]:
        """Resolve a migration conflict by promoting one of the alternatives."""
        rule = self.get(rule_id)
        if rule is None:
            raise NamingRuleError(f"Unknown rule: {rule_id}")
        language = language or self.language
        current = rule["targets"].get(language)
        options = set(rule.get("alternatives", []))
        if current:
            options.add(current)
        if value not in options:
            raise NamingRuleError("Value is not one of the recorded alternatives")
        rule["targets"][language] = value
        rule["alternatives"] = sorted(options - {value})
        report = self.data.get("migration") or {}
        for conflict in report.get("conflicts", []):
            if conflict["rule_id"] == rule_id:
                conflict["chosen"] = value
                conflict["resolved"] = True
        self.save()
        return rule

    # ------------------------------------------------------- legacy compatibility

    def legacy_user_mappings(self, language: Optional[str] = None) -> Dict[str, str]:
        """Flat ``{canonical key: target}`` view for callers that still think in mappings."""
        language = language or self.language
        view: Dict[str, str] = {}
        for rule in self.rules:
            if rule.get("filters"):
                # A rule that reaches only some entities cannot be flattened
                # into a view that has no room to say which.
                continue
            target = rule["targets"].get(language)
            if target:
                view[rule["match"]["value"]] = target
        return view

    def snapshot(self) -> Dict[str, Any]:
        return deepcopy(self.data)
