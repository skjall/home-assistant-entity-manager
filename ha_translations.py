"""Home Assistant's own entity names, in whatever language it speaks.

Home Assistant ships translated names for every device class and for the
translation keys its integrations declare, in around sixty languages. Asking
it beats keeping our own tables: a user whose Home Assistant speaks Italian
gets "Potenza reattiva" without anyone writing a rule for it.

The names are fetched once per language and kept in memory; they only change
when Home Assistant itself is updated.
"""

import logging
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

# "component.<domain>.entity_component.<device_class>.name"
_COMPONENT = "component.{domain}.entity_component.{key}.name"
# "component.<platform>.entity.<domain>.<translation_key>.name"
_ENTITY = "component.{platform}.entity.{domain}.{key}.name"


class HaTranslations:
    """Names Home Assistant uses for device classes and translation keys."""

    def __init__(self) -> None:
        self._component: Dict[str, Dict[str, str]] = {}
        self._entity: Dict[str, Dict[str, str]] = {}
        self._integrations: Dict[str, set] = {}
        # Built once per language from _component; see _names_by_key.
        self._by_key: Dict[str, Dict[str, str]] = {}

    @property
    def languages(self) -> Iterable[str]:
        return self._component.keys()

    def loaded_for(self, language: str, integrations: Iterable[str]) -> bool:
        """True when nothing new would be fetched for this language."""
        if language not in self._component:
            return False
        known = self._integrations.get(language, set())
        return set(integrations or ()) <= known

    async def load(self, websocket: Any, language: str, integrations: Iterable[str] = ()) -> None:
        """Fetch the names for ``language``; integrations are fetched on top."""
        wanted = {name for name in integrations if name}
        if self.loaded_for(language, wanted):
            return
        if language not in self._component:
            resources = await self._ask(websocket, language, "entity_component")
            if resources is None:
                return
            self._component[language] = resources
            self._by_key.pop(language, None)
            self._entity.setdefault(language, {})
            self._integrations.setdefault(language, set())
        missing = wanted - self._integrations[language]
        if missing:
            resources = await self._ask(websocket, language, "entity", sorted(missing))
            if resources is not None:
                self._entity[language].update(resources)
                self._integrations[language].update(missing)

    @staticmethod
    async def _ask(
        websocket: Any, language: str, category: str, integrations: Optional[list] = None
    ) -> Optional[Dict[str, str]]:
        message: Dict[str, Any] = {
            "type": "frontend/get_translations",
            "language": language,
            "category": category,
        }
        if integrations:
            message["integration"] = integrations
        try:
            msg_id = await websocket._send_message(message)
            response = await websocket._receive_message()
            while response.get("id") != msg_id:
                response = await websocket._receive_message()
        except Exception as error:  # a missing name must never break the naming
            logger.warning("Could not read Home Assistant translations (%s): %s", category, error)
            return None
        if not response.get("success"):
            logger.warning("Home Assistant refused the translations (%s): %s", category, response.get("error"))
            return None
        return (response.get("result") or {}).get("resources", {})

    @staticmethod
    def _usable(name: Optional[str]) -> Optional[str]:
        """Home Assistant fills placeholders like "Warnung {slot_id}" itself; we cannot."""
        if not name or "{" in name or "}" in name:
            return None
        return name

    def device_class_name(self, domain: str, device_class: str, language: str) -> Optional[str]:
        """What Home Assistant calls this device class, e.g. door -> "Tür"."""
        if not domain or not device_class:
            return None
        return self._usable(self._component.get(language, {}).get(_COMPONENT.format(domain=domain, key=device_class)))

    def translation_key_name(self, platform: str, domain: str, key: str, language: str) -> Optional[str]:
        """What the integration calls this entity, e.g. matter/reactive_current."""
        if not platform or not domain or not key:
            return None
        return self._usable(
            self._entity.get(language, {}).get(_ENTITY.format(platform=platform, domain=domain, key=key))
        )

    def domain_name(self, domain: str, language: str) -> Optional[str]:
        """The generic name of a domain, used when nothing more specific exists."""
        return self.device_class_name(domain, "_", language)

    def _component_keys(self, language: str):
        """Every (domain, key) pair Home Assistant translates for a language."""
        for resource in self._component.get(language, {}):
            parts = resource.split(".")
            if len(parts) >= 5 and parts[0] == "component" and parts[2] == "entity_component" and parts[-1] == "name":
                yield parts[1], ".".join(parts[3:-1])

    def device_classes(self, language: Optional[str] = None) -> set:
        """Every device class Home Assistant knows, across all its domains.

        This is what makes a value a device class rather than a name, and it
        comes from Home Assistant instead of a list kept here.
        """
        languages = [language] if language else list(self._component)
        classes = set()
        for lang in languages:
            classes.update(key for _, key in self._component_keys(lang) if key != "_")
        return classes

    def _names_by_key(self, language: str) -> Dict[str, str]:
        """Every device class of a language with its name, indexed once.

        Walking all 800 resources for every lookup cost a tenth of a second per
        request on a real home; the answer never changes between loads, so it
        is built once and kept until new translations arrive.
        """
        indexed = self._by_key.get(language)
        if indexed is not None:
            return indexed
        resources = self._component.get(language, {})
        indexed = {}
        for domain, key in self._component_keys(language):
            if key in indexed:
                continue
            name = self._usable(resources.get(_COMPONENT.format(domain=domain, key=key)))
            if name:
                indexed[key] = name
        self._by_key[language] = indexed
        return indexed

    def name_for_key(self, key: str, language: str) -> Optional[str]:
        """The name of a device class without knowing its domain.

        Domains agree on the common classes, so the first usable name wins; a
        domain that is sure of itself asks device_class_name instead.
        """
        if not key:
            return None
        return self._names_by_key(language).get(key)
