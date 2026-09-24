#!/usr/bin/env python3
"""The entities the energy dashboard is built out of.

The dashboard's settings live in ``.storage/energy`` and are reachable over the
WebSocket API alone - ``energy/get_prefs`` and ``energy/save_prefs``. The REST
API answers 404 for them, so a search that goes through files or REST never
finds them. What they hold are bare entity ids as text, with no link to the
registry: unlike an automation, nothing carries a rename into them. Rename the
entity and the dashboard goes on naming one that no longer exists - it shows
nothing, and only an external check ever says why.

Two things about writing them back:

* There is no partial save. ``save_prefs`` takes the whole object and validates
  it strictly, so what goes back has to be what came out, changed in place.
* The dashboard's own settings page saves the same way, sending the list as its
  browser read it. A page left open will therefore write over a change made
  here. The prefs are read again immediately before every write, which keeps
  the window as short as it can be made from this side.
"""

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from ha_websocket import HomeAssistantWebSocket

logger = logging.getLogger(__name__)


def ws_url_for(base_url: str) -> str:
    """The WebSocket address of Home Assistant, derived from its HTTP one."""
    return base_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"


# The fields that hold a statistic id. A statistic id is an entity id wherever
# the statistic comes from an entity; the external ones - "shellyplug:total" and
# its kin - carry a colon and match no entity, so they pass through untouched.
#
# Named rather than guessed, because the prefs also hold text that is not an id:
# a name, a currency. Every one of these appears in the schemas the energy
# integration validates against (homeassistant/components/energy/data.py).
STATISTIC_FIELDS = (
    "stat_energy_from",
    "stat_energy_to",
    "stat_cost",
    "stat_compensation",
    "entity_energy_price",
    "stat_rate",
    "stat_consumption",
    "included_in_stat",
)


def _walk(node: Any, path: str = "") -> List[Tuple[str, str, Dict[str, Any], str]]:
    """Every statistic id in the prefs, as (path, field, holder, value).

    Walked rather than reached for by name: "energy_sources" is a list of mixed
    types whose fields differ per type, and a grid source keeps its own lists of
    flows inside it. Walking finds them all, and a source type added to Home
    Assistant later is found too, without this having to learn its shape.
    """
    found: List[Tuple[str, str, Dict[str, Any], str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            if key in STATISTIC_FIELDS and isinstance(value, str) and value:
                found.append((here, key, node, value))
            else:
                found.extend(_walk(value, here))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_walk(value, f"{path}[{index}]"))
    return found


class EnergyPrefs:
    """Reads and rewrites the energy dashboard's settings."""

    def __init__(
        self,
        ws_url: str,
        token: str,
        command: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> None:
        self.ws_url = ws_url
        self.token = token
        # One way in, so a test can answer for Home Assistant without a socket.
        self._command = command or self._over_the_socket
        # One read per job: a device rename asks for every one of its entities,
        # and a read-only question does not need the settings again for each.
        # A write reads them afresh regardless; see the module docstring.
        self._prefs: Optional[Dict[str, Any]] = None

    async def _over_the_socket(self, message: Dict[str, Any]) -> Any:
        ws = HomeAssistantWebSocket(self.ws_url, self.token)
        await ws.connect()
        try:
            return await ws.send_command(message)
        finally:
            await ws.disconnect()

    async def prefs(self, fresh: bool = False) -> Dict[str, Any]:
        """The dashboard's settings as they stand."""
        if fresh or self._prefs is None:
            self._prefs = await self._command({"type": "energy/get_prefs"}) or {}
        return self._prefs

    async def save(self, prefs: Dict[str, Any]) -> None:
        """Put the settings back whole; there is no partial save."""
        await self._command({"type": "energy/save_prefs", **prefs})
        self._prefs = prefs

    @staticmethod
    def references(prefs: Dict[str, Any], entity_id: str) -> List[str]:
        """Where these settings name this entity, by path."""
        return [path for path, _, _, value in _walk(prefs) if value == entity_id]

    async def referring_to(self, entity_id: str) -> List[str]:
        """The places in the dashboard that name this entity."""
        return self.references(await self.prefs(), entity_id)

    async def broken(self, existing: set) -> List[Dict[str, str]]:
        """Places naming an entity that is not there any more.

        ``existing`` is every entity id Home Assistant knows. A statistic id
        from outside the registry carries a colon rather than a dot and is not
        an entity at all, so it is not missing either.
        """
        found = []
        for path, field, _, value in _walk(await self.prefs()):
            if ":" in value or "." not in value or value in existing:
                continue
            found.append({"path": path, "field": field, "missing_entity_id": value})
        return found

    async def rename(self, old_entity_id: str, new_entity_id: str) -> Dict[str, List[str]]:
        """Carry a rename into the dashboard wherever it named the old id."""
        results: Dict[str, List[str]] = {"success": [], "failed": []}
        # Read again, however recently it was read: the dashboard's own settings
        # page writes the whole list back as its browser had it, so anything
        # read a moment ago may already be behind.
        prefs = await self.prefs(fresh=True)
        naming = [(path, holder, field) for path, field, holder, value in _walk(prefs) if value == old_entity_id]
        if not naming:
            return results

        for _, holder, field in naming:
            holder[field] = new_entity_id
        try:
            await self.save(prefs)
        except Exception as error:  # noqa: BLE001 - a rename must not fail over the dashboard
            results["failed"] = [path for path, _, _ in naming]
            # Not kept: what is in memory now is what was asked for, not what
            # Home Assistant holds, and the next entity of the same job would
            # build on it.
            self._prefs = None
            logger.error("The energy dashboard kept %s: %s", old_entity_id, error)
            return results

        results["success"] = [path for path, _, _ in naming]
        logger.info("The energy dashboard now names %s in %d place(s)", new_entity_id, len(naming))
        return results
