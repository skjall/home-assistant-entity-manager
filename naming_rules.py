"""Type rules: what the entity-specific part of a name is called.

A rule maps an entity's type — identified by its integration's
``translation_key``, the canonical form of the name the integration supplies,
or its ``device_class`` — to the wording the user wants, per language.

Storage: ``/data/naming_rules.json``::

    {"version": 1, "language": "de",
     "rules": [{"id": "r_ab12cd34",
                "match": {"kind": "name", "value": "linkquality", "integration": null},
                "targets": {"de": "Verbindungsqualität"},
                "source": "learned", "learned_from": "sensor.x", "created_at": "..."}],
     "migration": {...}}

The legacy store ``user_type_mappings.json`` is migrated on first load; its
backup and a report stay next to it so nothing is lost silently.
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import shutil
from typing import Any, Dict, Iterable, List, Mapping, Optional
import uuid

from naming_canon import canon

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
KINDS = ("translation_key", "name", "device_class")
# Lookup order: the most specific identity first.
KIND_PRIORITY = {"translation_key": 0, "name": 1, "device_class": 2}


class NamingRuleError(ValueError):
    """Raised for an invalid rule."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return f"r_{uuid.uuid4().hex[:8]}"


class NamingRules:
    """Validate, resolve and persist type rules."""

    def __init__(
        self,
        storage_path: str = "/data/naming_rules.json",
        legacy_path: Optional[str] = None,
        device_class_keys: Iterable[str] = (),
        default_language: str = "en",
    ) -> None:
        self.storage_path = Path(storage_path)
        self.legacy_path = Path(legacy_path) if legacy_path else None
        self.device_class_keys = {canon(key) for key in device_class_keys}
        self.default_language = default_language
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    # ------------------------------------------------------------------ storage

    def _default_data(self) -> Dict[str, Any]:
        return {"version": SCHEMA_VERSION, "language": self.default_language, "rules": [], "migration": None}

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
                return data
            except (OSError, json.JSONDecodeError, NamingRuleError) as error:
                logger.error("Failed to load naming rules: %s", error)
                return self._default_data()

        data = self._default_data()
        if self.legacy_path and self.legacy_path.exists():
            self._migrate_legacy(data)
            self._write(data)
        return data

    def _write(self, data: Mapping[str, Any]) -> None:
        temporary = self.storage_path.with_suffix(self.storage_path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(data, file, indent=2, ensure_ascii=False)
        temporary.replace(self.storage_path)

    def save(self) -> None:
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
        }
        for (kind, key), group in sorted(grouped.items()):
            values = list(group["values"])
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

    # ------------------------------------------------------------------- rules

    @staticmethod
    def _make_rule(
        kind: str,
        value: str,
        integration: Optional[str],
        targets: Mapping[str, str],
        source: str = "user",
        learned_from: Optional[str] = None,
    ) -> Dict[str, Any]:
        if kind not in KINDS:
            raise NamingRuleError(f"Unknown rule kind: {kind}")
        if not value:
            raise NamingRuleError("A rule needs a match value")
        clean_targets = {lang: text.strip() for lang, text in targets.items() if isinstance(text, str) and text.strip()}
        if not clean_targets:
            raise NamingRuleError("A rule needs at least one target")
        rule = {
            "id": _new_id(),
            "match": {"kind": kind, "value": value, "integration": integration or None},
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

    def _matching(self, kind: str, value: str, integration: Optional[str]) -> Optional[Dict[str, Any]]:
        for rule in self.rules:
            match = rule["match"]
            if match["kind"] == kind and match["value"] == value and (match.get("integration") or None) == integration:
                return rule
        return None

    def find(
        self, kind: str, value: Optional[str], integration: Optional[str], language: str
    ) -> Optional[Dict[str, Any]]:
        """Return the rule for ``kind``/``value``, integration-specific before global."""
        if not value:
            return None
        key = value if kind == "translation_key" else canon(value)
        if not key:
            return None
        for scope in ((integration, None) if integration else (None,)):
            rule = self._matching(kind, key, scope)
            if rule and rule["targets"].get(language):
                return rule
        return None

    def upsert(
        self,
        kind: str,
        value: str,
        integration: Optional[str],
        language: str,
        target: str,
        source: str = "user",
        learned_from: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create or update the rule for one match, setting its target for ``language``."""
        key = value if kind == "translation_key" else canon(value)
        rule = self._matching(kind, key, integration or None)
        if rule is None:
            rule = self._make_rule(kind, key, integration, {language: target}, source, learned_from)
            self.rules.append(rule)
        else:
            if not target.strip():
                raise NamingRuleError("A rule needs a target")
            rule["targets"][language] = target.strip()
            rule["updated_at"] = _now()
            if learned_from:
                rule["learned_from"] = learned_from
        self.save()
        return rule

    def update(
        self, rule_id: str, targets: Optional[Mapping[str, str]] = None, integration: Any = ...
    ) -> Dict[str, Any]:
        rule = self.get(rule_id)
        if rule is None:
            raise NamingRuleError(f"Unknown rule: {rule_id}")
        if targets is not None:
            clean = {lang: text.strip() for lang, text in targets.items() if isinstance(text, str) and text.strip()}
            if not clean:
                raise NamingRuleError("A rule needs at least one target")
            rule["targets"] = clean
        if integration is not ...:
            rule["match"]["integration"] = integration or None
        rule["updated_at"] = _now()
        self.save()
        return rule

    def delete(self, rule_id: str) -> bool:
        before = len(self.rules)
        self.data["rules"] = [rule for rule in self.rules if rule["id"] != rule_id]
        if len(self.rules) == before:
            return False
        self.save()
        return True

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
            if rule["match"].get("integration"):
                continue
            target = rule["targets"].get(language)
            if target:
                view[rule["match"]["value"]] = target
        return view

    def snapshot(self) -> Dict[str, Any]:
        return deepcopy(self.data)
