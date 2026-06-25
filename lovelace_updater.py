#!/usr/bin/env python3
"""
Lovelace Updater - liest/schreibt Dashboard-Konfigurationen über WebSocket.

Home Assistant stellt Lovelace nur über WebSocket bereit (kein REST). Nur
Dashboards im Storage-Mode sind editierbar; YAML-Mode-Dashboards werden
übersprungen (und müssen manuell angepasst werden).

Wird für den Geräte-Austausch genutzt:
- update_all_dashboards(): biegt Entity-Referenzen in allen Storage-Dashboards um.
- get_referenced_entity_ids(): liefert alle in Dashboards referenzierten entity_ids
  (für die in-use-Erkennung, damit auch dashboard-only-Entities gemappt werden).
"""

import logging
from typing import Any, Dict, List, Optional, Set

from entity_ref_utils import extract_entity_ids, replace_entity_in_obj
from ha_websocket import HomeAssistantWebSocket

logger = logging.getLogger(__name__)


class LovelaceUpdater:
    """Liest/schreibt Lovelace-Dashboards über die HA-WebSocket-API."""

    def __init__(self, websocket: HomeAssistantWebSocket):
        self.ws = websocket

    async def _call(self, message: Dict[str, Any]) -> Dict[str, Any]:
        msg_id = await self.ws._send_message(message)
        response = await self.ws._receive_message()
        while response.get("id") != msg_id:
            response = await self.ws._receive_message()
        return response

    async def list_dashboards(self) -> List[Dict[str, Any]]:
        """Benutzerdefinierte Dashboards (url_path, mode, title)."""
        resp = await self._call({"type": "lovelace/dashboards/list"})
        if not resp.get("success"):
            logger.warning("lovelace/dashboards/list failed: %s", resp.get("error"))
            return []
        return resp.get("result", []) or []

    async def get_config(self, url_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Config eines Dashboards (url_path=None = Standard-Dashboard). None bei Fehler/YAML-Mode."""
        msg: Dict[str, Any] = {"type": "lovelace/config"}
        if url_path:
            msg["url_path"] = url_path
        resp = await self._call(msg)
        if not resp.get("success"):
            return None
        return resp.get("result")

    async def save_config(self, url_path: Optional[str], config: Dict[str, Any]) -> bool:
        msg: Dict[str, Any] = {"type": "lovelace/config/save", "config": config}
        if url_path:
            msg["url_path"] = url_path
        resp = await self._call(msg)
        if not resp.get("success"):
            logger.warning("lovelace/config/save (%s) failed: %s", url_path or "default", resp.get("error"))
        return bool(resp.get("success"))

    async def _storage_targets(self) -> List[Optional[str]]:
        """Editierbare Dashboards: Standard (None) + alle Storage-Mode-Dashboards."""
        targets: List[Optional[str]] = [None]
        for d in await self.list_dashboards():
            if d.get("mode") == "storage" and d.get("url_path"):
                targets.append(d.get("url_path"))
        return targets

    async def _all_targets(self) -> List[Optional[str]]:
        """Alle Dashboards (Standard + benutzerdefiniert, inkl. YAML) - nur zum Lesen."""
        targets: List[Optional[str]] = [None]
        for d in await self.list_dashboards():
            if d.get("url_path"):
                targets.append(d.get("url_path"))
        return targets

    async def scan_renames(self, rename_pairs: List) -> List[Dict[str, str]]:
        """Findet, in welchen Dashboards die alten IDs noch vorkommen.

        Nach dem automatischen Umbiegen (Storage) enthalten nur die nicht
        schreibbaren (YAML-Mode) Dashboards die alten IDs noch -> daraus wird die
        manuelle To-Do-Liste. Daher NACH update_all_dashboards aufrufen.
        """
        results: List[Dict[str, str]] = []
        for url_path in await self._all_targets():
            try:
                config = await self.get_config(url_path)
                if not isinstance(config, dict):
                    continue
                present = extract_entity_ids(config)
                for old, new in rename_pairs:
                    if old in present:
                        results.append({"dashboard": url_path or "default", "old": old, "new": new})
            except Exception as e:  # noqa: BLE001
                logger.warning("Dashboard %s scan_renames failed: %s", url_path or "default", e)
        return results

    async def update_all_dashboards(self, old_entity_id: str, new_entity_id: str) -> List[str]:
        """Ersetzt old->new in allen Storage-Dashboards. Gibt geänderte url_paths zurück."""
        changed: List[str] = []
        for url_path in await self._storage_targets():
            try:
                config = await self.get_config(url_path)
                if not isinstance(config, dict):
                    continue  # YAML-Mode / leer / nicht editierbar
                if replace_entity_in_obj(config, old_entity_id, new_entity_id):
                    if await self.save_config(url_path, config):
                        changed.append(url_path or "default")
            except Exception as e:  # noqa: BLE001 - ein Dashboard darf den Rest nicht stoppen
                logger.warning("Dashboard %s update failed: %s", url_path or "default", e)
        return changed

    async def get_referenced_entity_ids(self) -> Set[str]:
        """Alle in Dashboards (inkl. YAML, nur lesend) referenzierten entity_ids - für in-use."""
        referenced: Set[str] = set()
        for url_path in await self._all_targets():
            try:
                config = await self.get_config(url_path)
                if isinstance(config, dict):
                    referenced |= extract_entity_ids(config)
            except Exception as e:  # noqa: BLE001
                logger.warning("Dashboard %s scan failed: %s", url_path or "default", e)
        return referenced
