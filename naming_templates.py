"""Persistent, safe naming templates for devices and entities."""

from copy import deepcopy
import json
import logging
from pathlib import Path
import re
from string import Formatter
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from hierarchy_manager import normalize_name

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_TEMPLATE_LENGTH = 255

ALLOWED_FIELDS = frozenset(
    {
        "floor",
        "floor_id",
        "area",
        "area_id",
        "device",
        "device_id",
        "entity",
        "entity_id",
        "domain",
        "device_class",
        "manufacturer",
        "model",
        "integration",
    }
)

PRESETS = {
    "entity_manager": {
        "label": "Entity Manager",
        "templates": {
            "device_name": "{area} {device}",
            "entity_name": "{area} {device} {entity}",
            "entity_id": "{area} {device} {entity}",
        },
    },
    "home_assistant": {
        "label": "Home Assistant",
        "templates": {
            "device_name": "{device}",
            "entity_name": "{entity}",
            "entity_id": "{device} {entity}",
        },
    },
}

DEFAULT_PRESET = "entity_manager"
DEFAULT_TEMPLATES = PRESETS[DEFAULT_PRESET]["templates"]
TEMPLATE_KEYS = frozenset(DEFAULT_TEMPLATES)


class NamingTemplateError(ValueError):
    """Raised when a naming template is invalid."""


def _field_names(template: str) -> Tuple[str, ...]:
    """Return placeholder names from a format template."""
    try:
        return tuple(field for _, field, _, _ in Formatter().parse(template) if field)
    except ValueError as error:
        raise NamingTemplateError(f"Invalid template syntax: {error}") from error


def _clean_rendered_name(value: str) -> str:
    """Clean whitespace and separators left behind by empty placeholders."""
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"(?:\s*[-–—|/·]\s*){2,}", " - ", value)
    return value.strip(" \t\r\n_-–—|/·.,")


class NamingTemplates:
    """Validate, render, and persist the configured naming templates."""

    def __init__(self, storage_path: str = "naming_templates.json") -> None:
        """Initialize naming templates from ``storage_path``."""
        self.storage_path = Path(storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load_data()

    def _default_data(self) -> Dict[str, Any]:
        """Return a fresh default configuration."""
        return {
            "version": SCHEMA_VERSION,
            "preset": DEFAULT_PRESET,
            "templates": deepcopy(DEFAULT_TEMPLATES),
            "history": [],
        }

    def _load_data(self) -> Dict[str, Any]:
        """Load stored templates, falling back safely to Entity Manager defaults."""
        if not self.storage_path.exists():
            return self._default_data()
        try:
            with self.storage_path.open("r", encoding="utf-8") as file:
                data = json.load(file)
            templates = data.get("templates", {})
            self.validate_templates(templates)
            data["version"] = SCHEMA_VERSION
            data.setdefault("preset", self.matching_preset(templates) or "custom")
            data.setdefault("history", [])
            return data
        except (OSError, json.JSONDecodeError, NamingTemplateError) as error:
            logger.error("Failed to load naming templates: %s", error)
            return self._default_data()

    def _save_data(self) -> None:
        """Atomically persist the current configuration."""
        temporary_path = self.storage_path.with_suffix(self.storage_path.suffix + ".tmp")
        with temporary_path.open("w", encoding="utf-8") as file:
            json.dump(self.data, file, indent=2, ensure_ascii=False)
        temporary_path.replace(self.storage_path)

    @staticmethod
    def validate_template(template: str) -> None:
        """Validate one template's syntax and placeholders."""
        if not isinstance(template, str) or not template.strip():
            raise NamingTemplateError("Templates must be non-empty strings")
        if len(template) > MAX_TEMPLATE_LENGTH:
            raise NamingTemplateError(f"Templates may not exceed {MAX_TEMPLATE_LENGTH} characters")
        fields = _field_names(template)
        if not fields:
            raise NamingTemplateError("Templates must contain at least one placeholder")
        unknown = sorted(set(fields) - ALLOWED_FIELDS)
        if unknown:
            raise NamingTemplateError(f"Unknown placeholders: {', '.join(unknown)}")

    @classmethod
    def validate_templates(cls, templates: Mapping[str, str]) -> None:
        """Validate a complete template set."""
        if not isinstance(templates, Mapping):
            raise NamingTemplateError("Templates must be an object")
        missing = sorted(TEMPLATE_KEYS - set(templates))
        extra = sorted(set(templates) - TEMPLATE_KEYS)
        if missing:
            raise NamingTemplateError(f"Missing templates: {', '.join(missing)}")
        if extra:
            raise NamingTemplateError(f"Unknown template types: {', '.join(extra)}")
        for template in templates.values():
            cls.validate_template(template)
        if "device" not in _field_names(templates["device_name"]):
            raise NamingTemplateError("The device-name template must contain {device}")
        if "entity" not in _field_names(templates["entity_name"]):
            raise NamingTemplateError("The entity-name template must contain {entity}")
        if not {"device", "entity"}.intersection(_field_names(templates["entity_id"])):
            raise NamingTemplateError("The entity-ID template must contain {device} or {entity}")

    @staticmethod
    def matching_preset(templates: Mapping[str, str]) -> Optional[str]:
        """Return the preset matching ``templates``, if any."""
        for preset_id, preset in PRESETS.items():
            if dict(templates) == preset["templates"]:
                return preset_id
        return None

    def get_templates(self) -> Dict[str, str]:
        """Return a copy of the active templates."""
        return deepcopy(self.data["templates"])

    def get_config(self) -> Dict[str, Any]:
        """Return the public configuration used by the Settings UI."""
        return {
            "preset": self.data.get("preset", "custom"),
            "templates": self.get_templates(),
            "presets": deepcopy(PRESETS),
            "allowed_fields": sorted(ALLOWED_FIELDS),
        }

    def set_templates(self, templates: Mapping[str, str], preset: Optional[str] = None) -> Dict[str, Any]:
        """Validate and save a custom or preset template set."""
        clean_templates = {key: value.strip() for key, value in templates.items()}
        self.validate_templates(clean_templates)
        previous = self.get_templates()
        if previous != clean_templates:
            history = self.data.setdefault("history", [])
            if previous not in history:
                history.insert(0, previous)
            del history[5:]
        matched = self.matching_preset(clean_templates)
        self.data.update(
            {
                "version": SCHEMA_VERSION,
                "preset": matched or (preset if preset in PRESETS else "custom"),
                "templates": clean_templates,
            }
        )
        self._save_data()
        return self.get_config()

    def apply_preset(self, preset: str) -> Dict[str, Any]:
        """Apply and persist a named preset."""
        if preset not in PRESETS:
            raise NamingTemplateError(f"Unknown preset: {preset}")
        return self.set_templates(PRESETS[preset]["templates"], preset=preset)

    def render(self, template_key: str, context: Mapping[str, Any], normalize: bool = False) -> str:
        """Render one active template with a safe, flat context."""
        if template_key not in TEMPLATE_KEYS:
            raise NamingTemplateError(f"Unknown template type: {template_key}")
        return self.render_template(self.data["templates"][template_key], context, normalize=normalize)

    @classmethod
    def render_template(cls, template: str, context: Mapping[str, Any], normalize: bool = False) -> str:
        """Render an already validated template without changing configuration."""
        cls.validate_template(template)
        values = {field: str(context.get(field) or "") for field in ALLOWED_FIELDS}
        rendered = template.format_map(values)
        cleaned = _clean_rendered_name(rendered)
        return normalize_name(cleaned) if normalize else cleaned

    def candidate_templates(self, template_key: str) -> Iterable[str]:
        """Yield active, historical, and built-in templates for parsing existing names."""
        seen = set()
        groups = [self.data.get("templates", {})]
        groups.extend(self.data.get("history", []))
        groups.extend(preset["templates"] for preset in PRESETS.values())
        for templates in groups:
            template = templates.get(template_key)
            if template and template not in seen:
                seen.add(template)
                yield template

    def extract_field(
        self,
        template_key: str,
        rendered_name: str,
        field_name: str,
        context: Mapping[str, Any],
    ) -> Optional[str]:
        """Extract one placeholder value from a name rendered by a known template."""
        if not rendered_name or field_name not in ALLOWED_FIELDS:
            return None
        candidates = list(self.candidate_templates(template_key))
        candidates.sort(
            key=lambda template: (
                sum(1 for field in _field_names(template) if field != field_name and context.get(field)),
                len(template),
            ),
            reverse=True,
        )
        for template in candidates:
            if _field_names(template).count(field_name) != 1:
                continue
            pattern_parts = []
            for literal, field, _, _ in Formatter().parse(template):
                pattern_parts.append(re.escape(literal))
                if not field:
                    continue
                if field == field_name:
                    pattern_parts.append(r"(?P<target>.+?)")
                else:
                    value = str(context.get(field) or "")
                    pattern_parts.append(re.escape(value))
            match = re.fullmatch("".join(pattern_parts), rendered_name.strip(), flags=re.IGNORECASE)
            if match:
                return _clean_rendered_name(match.group("target"))
        return None
