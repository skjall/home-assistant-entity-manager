#!/usr/bin/env python3
"""
Type Mappings System - Multi-layer translation management for entity types.

This module provides intelligent entity type translation with multiple layers:
1. User mappings (highest priority) - Simple string, user's preference
2. Integration defaults - Zigbee2MQTT, Hue, etc. specific types
3. System defaults - Standard device_class translations (multilingual)
4. Fallback - Capitalize the key

Example resolution for "battery" with German user preference:
1. Check user_mappings["battery"] -> "Batterieladung" (if set)
2. Check integration_defaults["zigbee2mqtt"]["battery"] -> (skip, not integration-specific)
3. Check system_defaults["device_class"]["battery"]["de"] -> "Batterie"
4. Fallback -> "Battery"
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from json_store import atomically, guarded, new_lock
from naming_canon import canon

logger = logging.getLogger(__name__)

# Names used when Home Assistant cannot be asked, for instance before the first
# connection. Home Assistant itself knows several hundred device classes in
# every language it speaks and is the source whenever it answers.
DEFAULT_SYSTEM_MAPPINGS = {
    "device_class": {
        # Sensors
        "temperature": {"en": "Temperature", "de": "Temperatur", "es": "Temperatura", "fr": "Température"},
        "humidity": {"en": "Humidity", "de": "Luftfeuchtigkeit", "es": "Humedad", "fr": "Humidité"},
        "battery": {"en": "Battery", "de": "Batterie", "es": "Batería", "fr": "Batterie"},
        "illuminance": {"en": "Illuminance", "de": "Helligkeit", "es": "Iluminancia", "fr": "Luminosité"},
        "power": {"en": "Power", "de": "Leistung", "es": "Potencia", "fr": "Puissance"},
        "energy": {"en": "Energy", "de": "Energie", "es": "Energía", "fr": "Énergie"},
        "voltage": {"en": "Voltage", "de": "Spannung", "es": "Voltaje", "fr": "Tension"},
        "current": {"en": "Current", "de": "Strom", "es": "Corriente", "fr": "Courant"},
        "pressure": {"en": "Pressure", "de": "Druck", "es": "Presión", "fr": "Pression"},
        "co2": {"en": "CO2", "de": "CO2", "es": "CO2", "fr": "CO2"},
        "pm25": {"en": "PM2.5", "de": "PM2.5", "es": "PM2.5", "fr": "PM2.5"},
        "pm10": {"en": "PM10", "de": "PM10", "es": "PM10", "fr": "PM10"},
        "signal_strength": {
            "en": "Signal Strength",
            "de": "Signalstärke",
            "es": "Intensidad de Señal",
            "fr": "Force du Signal",
        },
        "timestamp": {"en": "Timestamp", "de": "Zeitstempel", "es": "Marca de Tiempo", "fr": "Horodatage"},
        "duration": {"en": "Duration", "de": "Dauer", "es": "Duración", "fr": "Durée"},
        # Binary sensors
        "motion": {"en": "Motion", "de": "Bewegung", "es": "Movimiento", "fr": "Mouvement"},
        "occupancy": {"en": "Occupancy", "de": "Anwesenheit", "es": "Ocupación", "fr": "Occupation"},
        "door": {"en": "Door", "de": "Tür", "es": "Puerta", "fr": "Porte"},
        "window": {"en": "Window", "de": "Fenster", "es": "Ventana", "fr": "Fenêtre"},
        "smoke": {"en": "Smoke", "de": "Rauch", "es": "Humo", "fr": "Fumée"},
        "moisture": {"en": "Moisture", "de": "Feuchtigkeit", "es": "Humedad", "fr": "Humidité"},
        "connectivity": {"en": "Connectivity", "de": "Verbindung", "es": "Conectividad", "fr": "Connectivité"},
        "vibration": {"en": "Vibration", "de": "Vibration", "es": "Vibración", "fr": "Vibration"},
        "problem": {"en": "Problem", "de": "Problem", "es": "Problema", "fr": "Problème"},
        "safety": {"en": "Safety", "de": "Sicherheit", "es": "Seguridad", "fr": "Sécurité"},
        "tamper": {"en": "Tamper", "de": "Manipulation", "es": "Manipulación", "fr": "Sabotage"},
        "plug": {"en": "Plug", "de": "Stecker", "es": "Enchufe", "fr": "Prise"},
        "presence": {"en": "Presence", "de": "Präsenz", "es": "Presencia", "fr": "Présence"},
        "running": {"en": "Running", "de": "Läuft", "es": "En Funcionamiento", "fr": "En Marche"},
        "lock": {"en": "Lock", "de": "Schloss", "es": "Cerradura", "fr": "Verrou"},
        "opening": {"en": "Opening", "de": "Öffnung", "es": "Apertura", "fr": "Ouverture"},
        "garage_door": {"en": "Garage Door", "de": "Garagentor", "es": "Puerta de Garaje", "fr": "Porte de Garage"},
        # Domains
        "light": {"en": "Light", "de": "Licht", "es": "Luz", "fr": "Lumière"},
        "switch": {"en": "Switch", "de": "Schalter", "es": "Interruptor", "fr": "Interrupteur"},
        "climate": {"en": "Climate", "de": "Klima", "es": "Clima", "fr": "Climatisation"},
        "cover": {"en": "Cover", "de": "Abdeckung", "es": "Cubierta", "fr": "Couverture"},
        "fan": {"en": "Fan", "de": "Ventilator", "es": "Ventilador", "fr": "Ventilateur"},
        "media_player": {"en": "Media Player", "de": "Mediaplayer", "es": "Reproductor", "fr": "Lecteur Multimédia"},
        "sensor": {"en": "Sensor", "de": "Sensor", "es": "Sensor", "fr": "Capteur"},
        "binary_sensor": {"en": "Binary Sensor", "de": "Binärsensor", "es": "Sensor Binario", "fr": "Capteur Binaire"},
        "button": {"en": "Button", "de": "Taste", "es": "Botón", "fr": "Bouton"},
        "select": {"en": "Select", "de": "Auswahl", "es": "Selección", "fr": "Sélection"},
        "number": {"en": "Number", "de": "Zahl", "es": "Número", "fr": "Nombre"},
        "scene": {"en": "Scene", "de": "Szene", "es": "Escena", "fr": "Scène"},
        "script": {"en": "Script", "de": "Skript", "es": "Script", "fr": "Script"},
        "automation": {"en": "Automation", "de": "Automatisierung", "es": "Automatización", "fr": "Automatisation"},
        "update": {"en": "Update", "de": "Update", "es": "Actualización", "fr": "Mise à Jour"},
    },
    "integration_defaults": {
        "zigbee2mqtt": {
            "linkquality": {
                "en": "Link Quality",
                "de": "Verbindungsqualität",
                "es": "Calidad de Enlace",
                "fr": "Qualité de Liaison",
            },
            "update_available": {
                "en": "Update Available",
                "de": "Update verfügbar",
                "es": "Actualización Disponible",
                "fr": "Mise à Jour Disponible",
            },
            "occupancy_timeout": {
                "en": "Occupancy Timeout",
                "de": "Anwesenheits-Timeout",
                "es": "Tiempo de Ocupación",
                "fr": "Délai d'Occupation",
            },
            "action": {"en": "Action", "de": "Aktion", "es": "Acción", "fr": "Action"},
            "click": {"en": "Click", "de": "Klick", "es": "Clic", "fr": "Clic"},
            "sensitivity": {"en": "Sensitivity", "de": "Empfindlichkeit", "es": "Sensibilidad", "fr": "Sensibilité"},
            "led_indication": {
                "en": "LED Indication",
                "de": "LED-Anzeige",
                "es": "Indicación LED",
                "fr": "Indication LED",
            },
            "power_outage_memory": {
                "en": "Power Outage Memory",
                "de": "Stromausfall-Speicher",
                "es": "Memoria de Corte de Energía",
                "fr": "Mémoire de Coupure",
            },
            "child_lock": {
                "en": "Child Lock",
                "de": "Kindersicherung",
                "es": "Bloqueo Infantil",
                "fr": "Verrouillage Enfant",
            },
        },
        "hue": {
            "color_temp_startup": {
                "en": "Startup Color Temp",
                "de": "Start-Farbtemperatur",
                "es": "Temp. Color Inicio",
                "fr": "Temp. Couleur Démarrage",
            },
            "dynamics": {"en": "Dynamics", "de": "Dynamik", "es": "Dinámica", "fr": "Dynamique"},
        },
        "esphome": {
            "wifi_signal": {"en": "WiFi Signal", "de": "WLAN-Signal", "es": "Señal WiFi", "fr": "Signal WiFi"},
            "uptime": {"en": "Uptime", "de": "Betriebszeit", "es": "Tiempo Activo", "fr": "Temps de Fonctionnement"},
        },
    },
}


class TypeMappings:
    """
    Manages entity type translations with multiple priority layers.

    Resolution order:
    1. User mappings (highest priority) - stored in user_mappings.json
    2. Integration defaults - from system_mappings.json or embedded defaults
    3. System device_class defaults - from system_mappings.json or embedded
    4. Fallback - capitalize the key
    """

    def __init__(
        self,
        system_mappings_path: Optional[str] = None,
        user_mappings_path: str = "/data/user_type_mappings.json",
        rules: Optional[Any] = None,
        ha_translations: Optional[Any] = None,
    ):
        """
        Initialize the type mappings manager.

        Args:
            system_mappings_path: Path to system mappings JSON (optional, uses embedded defaults)
            user_mappings_path: Path to user mappings JSON (legacy store)
            rules: NamingRules store; when given, user mappings are a view on
                its rules for the active language and the legacy file is not used
        """
        # One lock per store: a read-change-write stays one step.
        self._lock = new_lock()
        self.system_mappings_path = Path(system_mappings_path) if system_mappings_path else None
        self.user_mappings_path = Path(user_mappings_path)
        self.rules = rules
        # Home Assistant's own names; asked before the embedded table.
        self.ha_translations = ha_translations

        self.system_mappings = self._load_system_mappings()
        self.user_mappings = self._load_user_mappings()

    def _refresh_user_view(self) -> None:
        """Rebuild the flat user-mapping view from the rules store."""
        if self.rules is not None:
            self.user_mappings = self.rules.legacy_user_mappings()

    def _load_system_mappings(self) -> Dict[str, Any]:
        """Load system mappings from file or use embedded defaults."""
        if self.system_mappings_path and self.system_mappings_path.exists():
            try:
                with open(self.system_mappings_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    logger.info(f"System mappings loaded from {self.system_mappings_path}")
                    return data
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Error loading system mappings: {e}")

        logger.info("Using embedded system mappings")
        return DEFAULT_SYSTEM_MAPPINGS

    def _load_user_mappings(self) -> Dict[str, str]:
        """Load user mappings from the rules store, or from the legacy file."""
        if self.rules is not None:
            return self.rules.legacy_user_mappings()
        if self.user_mappings_path.exists():
            try:
                with open(self.user_mappings_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("user_mappings", {})
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Error loading user mappings: {e}")

        return {}

    def _save_user_mappings(self) -> None:
        """Save user mappings to file (legacy store only)."""
        if self.rules is not None:
            return
        try:
            atomically(self.user_mappings_path, {"user_mappings": self.user_mappings})
            logger.info(f"User mappings saved: {len(self.user_mappings)} entries")
        except Exception as e:
            logger.error(f"Error saving user mappings: {e}")

    def get_translation(
        self,
        type_key: str,
        language: str = "en",
        integration: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> str:
        """
        Get the best translation for a type key.

        Resolution order:
        1. User mapping (highest priority)
        2. Integration-specific default
        3. System device_class default
        4. Domain default (if provided)
        5. Fallback to capitalized key

        Args:
            type_key: The type key to translate (e.g., "battery", "temperature")
            language: Target language code (e.g., "de", "en")
            integration: Optional integration name (e.g., "zigbee2mqtt")
            domain: Optional domain fallback (e.g., "sensor")

        Returns:
            Translated string for the type
        """
        if not type_key:
            return ""

        resolved = self.find_translation(type_key, language, integration, domain)
        if resolved is not None:
            return resolved

        # 5. Fallback: capitalize the key
        return type_key.replace("_", " ").title()

    def _supplied_name(self, key: str, language: str) -> Optional[str]:
        """What Home Assistant calls this device class, if it knows it."""
        if not key or self.ha_translations is None:
            return None
        return self.ha_translations.name_for_key(key, language)

    def find_system_translation(self, type_key: str, language: str, integration: Optional[str] = None) -> Optional[str]:
        """The default for ``type_key`` in ``language``, ignoring user rules."""
        if not type_key:
            return None
        key = canon(type_key)
        if integration:
            integration_mappings = self.system_mappings.get("integration_defaults", {}).get(integration, {})
            if key in integration_mappings:
                lang_mapping = integration_mappings[key]
                return lang_mapping.get(language, lang_mapping.get("en"))
        supplied = self._supplied_name(key, language)
        if supplied:
            return supplied
        device_class_mappings = self.system_mappings.get("device_class", {})
        if key in device_class_mappings:
            lang_mapping = device_class_mappings[key]
            return lang_mapping.get(language, lang_mapping.get("en"))
        return None

    def find_translation(
        self,
        type_key: str,
        language: str = "en",
        integration: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> Optional[str]:
        """
        Resolve steps 1-4 of ``get_translation``, or ``None`` when nothing matches.

        Callers that must distinguish a real translation from the capitalized
        fallback use this: a name Home Assistant already supplies is worth
        keeping when no mapping exists for it.

        Args:
            type_key: The type key to translate (e.g., "battery", "linkquality")
            language: Target language code (e.g., "de", "en")
            integration: Optional integration name (e.g., "zigbee2mqtt")
            domain: Optional domain fallback (e.g., "sensor")

        Returns:
            The mapped string, or None if no mapping covers ``type_key``
        """
        if not type_key:
            return None

        # 1. User rules (highest priority), integration-specific before global
        if self.rules is not None:
            rule = self.rules.find("name", type_key, integration, language)
            if rule is not None:
                return rule["targets"][language]
        elif type_key.lower() in self.user_mappings:
            return self.user_mappings[type_key.lower()]

        type_key_lower = canon(type_key)

        # 2. Check integration-specific defaults
        if integration:
            integration_mappings = self.system_mappings.get("integration_defaults", {}).get(integration, {})
            if type_key_lower in integration_mappings:
                lang_mapping = integration_mappings[type_key_lower]
                return lang_mapping.get(language, lang_mapping.get("en", type_key.title()))

        # 3. What Home Assistant calls this device class, then the embedded table
        supplied = self._supplied_name(type_key_lower, language)
        if supplied:
            return supplied
        device_class_mappings = self.system_mappings.get("device_class", {})
        if type_key_lower in device_class_mappings:
            lang_mapping = device_class_mappings[type_key_lower]
            return lang_mapping.get(language, lang_mapping.get("en", type_key.title()))

        # 4. Check domain as fallback
        if domain:
            domain_lower = domain.lower()
            supplied = self._supplied_name(domain_lower, language)
            if supplied:
                return supplied
            if domain_lower in device_class_mappings:
                lang_mapping = device_class_mappings[domain_lower]
                return lang_mapping.get(language, lang_mapping.get("en", domain.title()))

        return None

    @guarded
    def set_user_mapping(self, type_key: str, translation: str) -> None:
        """
        Set a user mapping for a type key.

        Args:
            type_key: The type key (e.g., "battery")
            translation: The user's preferred translation (e.g., "Batterieladung")
        """
        if self.rules is not None:
            self.rules.upsert("name", type_key, None, self.rules.language, translation)
            self._refresh_user_view()
            return
        type_key_lower = type_key.lower()
        self.user_mappings[type_key_lower] = translation
        self._save_user_mappings()
        logger.info(f"User mapping set: {type_key_lower} -> {translation}")

    @guarded
    def remove_user_mapping(self, type_key: str) -> bool:
        """
        Remove a user mapping.

        Args:
            type_key: The type key to remove

        Returns:
            True if removed, False if not found
        """
        if self.rules is not None:
            rule = self.rules.find("name", type_key, None, self.rules.language)
            removed = bool(rule) and self.rules.delete(rule["id"])
            self._refresh_user_view()
            return removed
        type_key_lower = type_key.lower()
        if type_key_lower in self.user_mappings:
            del self.user_mappings[type_key_lower]
            self._save_user_mappings()
            logger.info(f"User mapping removed: {type_key_lower}")
            return True
        return False

    def get_user_mapping(self, type_key: str) -> Optional[str]:
        """Get user mapping for a type key if it exists."""
        if self.rules is not None:
            rule = self.rules.find("name", type_key, None, self.rules.language)
            return rule["targets"][self.rules.language] if rule else None
        return self.user_mappings.get(type_key.lower())

    def get_all_user_mappings(self) -> Dict[str, str]:
        """Get all user mappings."""
        self._refresh_user_view()
        return self.user_mappings.copy()

    def get_all_known_types(self, language: str = "en") -> List[Dict[str, Any]]:
        """
        Get all known type keys with their translations.

        Returns a list of dicts with:
        - key: The type key
        - system_default: The system default translation
        - user_mapping: The user's custom mapping (if any)

        Args:
            language: Target language for system defaults

        Returns:
            List of type info dicts
        """
        self._refresh_user_view()
        all_types = []
        seen_keys = set()

        # Every device class Home Assistant knows, in its own words
        if self.ha_translations is not None:
            for type_key in sorted(self.ha_translations.device_classes(language)):
                name = self.ha_translations.name_for_key(type_key, language)
                if not name or type_key in seen_keys:
                    continue
                seen_keys.add(type_key)
                all_types.append(
                    {
                        "key": type_key,
                        "system_default": name,
                        "user_mapping": self.user_mappings.get(type_key),
                        "source": "device_class",
                    }
                )

        # Collect from device_class mappings
        for type_key, lang_mapping in self.system_mappings.get("device_class", {}).items():
            if type_key not in seen_keys:
                seen_keys.add(type_key)
                all_types.append(
                    {
                        "key": type_key,
                        "system_default": lang_mapping.get(language, lang_mapping.get("en", type_key.title())),
                        "user_mapping": self.user_mappings.get(type_key),
                        "source": "device_class",
                    }
                )

        # Collect from integration defaults
        for integration, mappings in self.system_mappings.get("integration_defaults", {}).items():
            for type_key, lang_mapping in mappings.items():
                if type_key not in seen_keys:
                    seen_keys.add(type_key)
                    all_types.append(
                        {
                            "key": type_key,
                            "system_default": lang_mapping.get(language, lang_mapping.get("en", type_key.title())),
                            "user_mapping": self.user_mappings.get(type_key),
                            "source": f"integration:{integration}",
                        }
                    )

        # Add user mappings not in system
        for type_key, translation in self.user_mappings.items():
            if type_key not in seen_keys:
                all_types.append(
                    {
                        "key": type_key,
                        "system_default": None,
                        "user_mapping": translation,
                        "source": "user_custom",
                    }
                )

        # Sort by key
        all_types.sort(key=lambda x: x["key"])

        return all_types

    def detect_integration(self, entity_id: str) -> Optional[str]:
        """
        Try to detect the integration from an entity ID.

        Args:
            entity_id: The entity ID to analyze

        Returns:
            Integration name if detected, None otherwise
        """
        entity_id_lower = entity_id.lower()

        # Common patterns
        if "zigbee2mqtt" in entity_id_lower or "0x" in entity_id_lower:
            return "zigbee2mqtt"
        if "hue" in entity_id_lower:
            return "hue"
        if "esphome" in entity_id_lower:
            return "esphome"
        if "tasmota" in entity_id_lower:
            return "tasmota"

        return None

    def get_system_default(self, type_key: str, language: str = "en") -> Optional[str]:
        """
        Get only the system default for a type key (ignoring user mappings).

        Args:
            type_key: The type key
            language: Target language

        Returns:
            System default translation or None
        """
        type_key_lower = type_key.lower()

        supplied = self._supplied_name(type_key_lower, language)
        if supplied:
            return supplied

        # Check device_class
        device_class_mappings = self.system_mappings.get("device_class", {})
        if type_key_lower in device_class_mappings:
            lang_mapping = device_class_mappings[type_key_lower]
            return lang_mapping.get(language, lang_mapping.get("en"))

        # Check all integrations
        for mappings in self.system_mappings.get("integration_defaults", {}).values():
            if type_key_lower in mappings:
                lang_mapping = mappings[type_key_lower]
                return lang_mapping.get(language, lang_mapping.get("en"))

        return None

    def has_user_mapping(self, type_key: str) -> bool:
        """Check if a user mapping exists for the type key."""
        return type_key.lower() in self.user_mappings

    @guarded
    def reload(self) -> None:
        """Reload mappings from files."""
        self.system_mappings = self._load_system_mappings()
        self.user_mappings = self._load_user_mappings()
        logger.info("Type mappings reloaded")
