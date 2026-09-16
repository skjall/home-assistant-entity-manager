#!/usr/bin/env python3
"""The options of helpers built in the Home Assistant interface.

A helper created through the interface - a template sensor, a Riemann sum, a
utility meter - is a config entry, not YAML. Home Assistant keeps entity fields
in those up to date by itself: rename an entity and the ``source`` of a Riemann
helper follows. What it cannot follow is free text. A template holds its entity
ids inside a Jinja string, and to Home Assistant that is prose. Rename the
entity and the helper keeps asking for a name nobody answers to any more - it
reports zero and says nothing.

Nothing about a config entry's options can be read straight out: the entry list
carries neither ``data`` nor ``options``. The only way in is to start an options
flow, which answers with every field and the value it currently holds, and to
abort it again - measured as leaving the entry untouched, field for field.

Writing goes back through the same flow, and one rule is easy to get wrong:
fields that have no value must be left out of the submission. Sent as null they
are refused, and the whole write fails.
"""

import logging
from typing import Any, Dict, List, Optional

import aiohttp

from entity_ref_utils import extract_entity_ids, replace_entity_in_obj

logger = logging.getLogger(__name__)

# Config entry domains that keep free text a rename cannot follow. Entity
# fields are left out on purpose: Home Assistant already carries those along,
# so rewriting them would be work for nothing and a chance to get it wrong.
TEMPLATE_HELPERS = ("template", "history_stats")


class HelperOptions:
    """Reads and rewrites the options of interface-built helpers."""

    def __init__(self, base_url: str, token: str, domains: tuple = TEMPLATE_HELPERS) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.domains = domains
        # One read per helper per job: a device rename asks for every one of its
        # entities, and the options do not change under us in between.
        self._options: Dict[str, Dict[str, Any]] = {}
        self._entries: Optional[List[Dict[str, Any]]] = None

    async def _request(self, method: str, path: str, payload: Optional[Dict] = None) -> Any:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method, f"{self.base_url}{path}", headers=self.headers, json=payload
            ) as response:
                if response.status >= 400:
                    raise RuntimeError(f"{method} {path} answered {response.status}: {await response.text()}")
                body = await response.text()
                return await response.json() if body else None

    async def entries(self) -> List[Dict[str, Any]]:
        """The helper config entries, read once."""
        if self._entries is None:
            found = await self._request("GET", "/api/config/config_entries/entry")
            self._entries = [
                entry
                for entry in found
                if entry.get("domain") in self.domains
                and entry.get("supports_options")
                and not entry.get("disabled_by")
            ]
            logger.info("Found %d helpers with templates", len(self._entries))
        return self._entries

    @staticmethod
    def _values_of(flow: Dict[str, Any]) -> Dict[str, Any]:
        return {
            field.get("name"): (field.get("description") or {}).get("suggested_value")
            for field in (flow.get("data_schema") or [])
            if field.get("name")
        }

    async def options_of(self, entry_id: str) -> Dict[str, Any]:
        """What this helper is configured with right now.

        Empty when the helper answers with something other than a form - a menu,
        for instance, which this cannot fill in blind.
        """
        if entry_id in self._options:
            return self._options[entry_id]

        flow = await self._request("POST", "/api/config/config_entries/options/flow", {"handler": entry_id})
        try:
            if flow.get("type") != "form":
                logger.info("Helper %s opens with a %s, leaving it alone", entry_id, flow.get("type"))
                values: Dict[str, Any] = {}
            else:
                values = self._values_of(flow)
        finally:
            if flow.get("flow_id"):
                # Aborting keeps the entry as it was; an abandoned flow would
                # otherwise sit in Home Assistant's list of pending ones.
                await self._request("DELETE", f"/api/config/config_entries/options/flow/{flow['flow_id']}")

        self._options[entry_id] = values
        return values

    async def write(self, entry_id: str, values: Dict[str, Any]) -> None:
        """Put these options back, through a fresh flow."""
        flow = await self._request("POST", "/api/config/config_entries/options/flow", {"handler": entry_id})
        # A field without a value is refused when it is sent as null, and one
        # rejected field fails the whole write.
        answer = await self._request(
            "POST",
            f"/api/config/config_entries/options/flow/{flow['flow_id']}",
            {key: value for key, value in values.items() if value is not None},
        )
        if answer.get("type") != "create_entry":
            raise RuntimeError(f"Helper {entry_id} refused the options: {answer.get('errors') or answer.get('reason')}")
        self._options[entry_id] = values

    async def referring_to(self, entity_id: str) -> List[Dict[str, Any]]:
        """Helpers whose options mention this entity."""
        found = []
        for entry in await self.entries():
            options = await self.options_of(entry["entry_id"])
            if options and entity_id in extract_entity_ids(options):
                found.append({"entry_id": entry["entry_id"], "title": entry.get("title") or "", "options": options})
        return found

    async def broken(self, existing: set) -> List[Dict[str, Any]]:
        """Helpers whose templates name an entity that is not there any more.

        ``existing`` is every entity id Home Assistant currently knows. Only ids
        whose domain occurs among them are considered: a template holds prose,
        and prose contains dotted words that are not entities.
        """
        domains = {entity_id.split(".", 1)[0] for entity_id in existing}
        found = []
        for entry in await self.entries():
            options = await self.options_of(entry["entry_id"])
            for field, value in (options or {}).items():
                if not isinstance(value, str):
                    continue
                for entity_id in sorted(extract_entity_ids(value)):
                    if entity_id.split(".", 1)[0] in domains and entity_id not in existing:
                        found.append(
                            {
                                "entry_id": entry["entry_id"],
                                "title": entry.get("title") or "",
                                "domain": entry.get("domain") or "",
                                "field": field,
                                "missing_entity_id": entity_id,
                            }
                        )
        return found

    async def replace_in(self, entry_id: str, old_entity_id: str, new_entity_id: str) -> bool:
        """Swap one entity id inside one helper. False when it was not there."""
        values = dict(await self.options_of(entry_id))
        if not replace_entity_in_obj(values, old_entity_id, new_entity_id):
            return False
        await self.write(entry_id, values)
        return True

    async def rename(self, old_entity_id: str, new_entity_id: str) -> Dict[str, List[str]]:
        """Carry a rename into every helper that spelled out the old id."""
        results: Dict[str, List[str]] = {"success": [], "failed": []}
        for helper in await self.referring_to(old_entity_id):
            values = dict(helper["options"])
            if not replace_entity_in_obj(values, old_entity_id, new_entity_id):
                continue
            try:
                await self.write(helper["entry_id"], values)
                results["success"].append(helper["title"])
                logger.info("Helper '%s' now asks for %s", helper["title"], new_entity_id)
            except Exception as error:  # noqa: BLE001 - one helper must not stop the rest
                results["failed"].append(helper["title"])
                logger.error("Helper '%s' kept the old id: %s", helper["title"], error)
        return results
