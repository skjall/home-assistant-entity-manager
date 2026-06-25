#!/usr/bin/env python3
"""
Integration Bridge - Abstraktion über die Quell-Integration eines Geräts.

Ein HA-Gerät stammt aus einer Integration (Zigbee2MQTT über MQTT, Matter, ZHA,
HomeKit, ...). Manche Operationen - vor allem Umbenennen und Entfernen - müssen
NATIV in der jeweiligen Integration passieren, nicht nur in der HA-Registry:

- Zigbee2MQTT führt seinen eigenen friendly_name (nur über MQTT änderbar). Ohne
  Angleichung divergieren HA und Z2M und man findet das Gerät in Z2M nicht mehr.
- Wird ein Gerät nur in HA gelöscht, aber in Z2M/Matter nicht entfernt, taucht es
  bei der nächsten Discovery wieder auf (Re-Population).

Diese Datei definiert die Abstraktion (Adapter-Interface + Dispatcher); die
konkreten Adapter liegen in bridge_adapters.py.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import logging
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class BridgeResult:
    """Ergebnis einer nativen Bridge-Operation.

    native_supported=False bedeutet: Der Adapter kennt für diese Integration
    keine native Operation (z.B. Matter hat keinen Eigennamen) - der Aufrufer
    soll dann auf die reine HA-Registry-Operation zurückfallen und das nicht
    als Fehler werten.
    """

    success: bool
    native_supported: bool
    detail: str = ""
    error: Optional[str] = None


def extract_integrations(device_data: Dict[str, Any]) -> List[str]:
    """Ermittelt die Integration(en) eines Geräts aus seinen identifiers.

    identifiers sieht z.B. so aus: [["mqtt", "zigbee2mqtt_0x.."], ["zha", "y"]].
    Manche Domains haben das Format "homekit_controller:accessory-id" - wir
    nehmen nur den Domain-Teil.

    Args:
        device_data: Ein Device-Eintrag aus config/device_registry/list.

    Returns:
        Liste der Integration-Domains (z.B. ["mqtt"], ["zha"], ["matter"]).
    """
    integrations: List[str] = []
    for identifier in device_data.get("identifiers", []):
        if isinstance(identifier, (list, tuple)) and len(identifier) >= 1:
            domain = identifier[0]
            if isinstance(domain, str) and ":" in domain:
                domain = domain.split(":")[0]
            if domain and domain not in integrations:
                integrations.append(domain)
    return integrations


def extract_config_entry_id(device_data: Dict[str, Any]) -> Optional[str]:
    """Liefert eine Config-Entry-ID des Geräts (für remove_config_entry).

    Die meisten Geräte haben genau einen Config-Entry. Bei mehreren wird der
    erste genommen und eine Warnung geloggt - die Domain-genaue Auflösung
    (config/config_entries/get) ist hier bewusst noch nicht implementiert.
    """
    config_entries = device_data.get("config_entries") or []
    if not config_entries:
        return None
    if len(config_entries) > 1:
        logger.warning(
            "Device %s has multiple config_entries %s - using the first one for removal",
            device_data.get("id"),
            config_entries,
        )
    return config_entries[0]


class IntegrationBridgeAdapter(ABC):
    """Basis für integrationsspezifische Geräte-Operationen."""

    integration_key: str = "registry"

    @abstractmethod
    def matches(self, integrations: List[str]) -> bool:
        """True, wenn dieser Adapter für die gegebenen Integrationen zuständig ist."""

    @abstractmethod
    async def rename_native(self, device_data: Dict[str, Any], new_name: str) -> BridgeResult:
        """Benennt das Gerät nativ in seiner Integration um."""

    @abstractmethod
    async def remove_native(self, device_data: Dict[str, Any], *, force: bool = False) -> BridgeResult:
        """Entfernt das Gerät nativ aus seiner Integration (verhindert Re-Population)."""

    @property
    @abstractmethod
    def capabilities(self) -> Set[str]:
        """Menge unterstützter nativer Operationen, Teilmenge von {"rename", "remove"}."""


class IntegrationBridge:
    """Wählt anhand der Geräte-Integration den passenden Adapter und delegiert."""

    def __init__(self, adapters: List[IntegrationBridgeAdapter]):
        if not adapters:
            raise ValueError("IntegrationBridge requires at least one adapter")
        # Die Reihenfolge bestimmt die Priorität; der letzte sollte ein catch-all sein.
        self._adapters = adapters

    def select_adapter(self, device_data: Dict[str, Any]) -> IntegrationBridgeAdapter:
        """Bestimmt den zuständigen Adapter für ein Gerät."""
        integrations = extract_integrations(device_data)
        for adapter in self._adapters:
            if adapter.matches(integrations):
                return adapter
        return self._adapters[-1]

    async def rename_native(self, device_data: Dict[str, Any], new_name: str) -> BridgeResult:
        """Native Umbenennung über den passenden Adapter."""
        return await self.select_adapter(device_data).rename_native(device_data, new_name)

    async def remove_native(self, device_data: Dict[str, Any], *, force: bool = False) -> BridgeResult:
        """Native Entfernung über den passenden Adapter."""
        return await self.select_adapter(device_data).remove_native(device_data, force=force)

    def capabilities_for(self, device_data: Dict[str, Any]) -> Set[str]:
        """Native Fähigkeiten für ein konkretes Gerät."""
        return self.select_adapter(device_data).capabilities
