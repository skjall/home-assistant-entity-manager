import logging
from typing import Any, Dict, List, Optional

from ha_websocket import HomeAssistantWebSocket
from label_registry import LabelRegistry
import supplied_names as supplied_names_module

logger = logging.getLogger(__name__)


class EntityRegistry:
    # Optional shared audit log for entity_id renames. Set once by the web app
    # at startup (see web_ui.py); stays None in contexts that don't wire it up.
    rename_log = None

    # Optional shared record of which names this add-on wrote. Every write goes
    # through update_entity, so noting it here is the one place that cannot be
    # forgotten when a new rename path appears. Set once by app_state.
    naming_state = None

    # Optional reader and writer of config entry titles. A helper built in the
    # Home Assistant interface is named by its entry's title, so renaming the
    # entity without it leaves the name the integration supplies behind - and
    # the next proposal is built from that stale name. Set once by app_state.
    supplied_names = None

    def __init__(self, websocket: HomeAssistantWebSocket):
        self.ws = websocket
        self.entities: Dict[str, Dict] = {}
        self.label_registry = LabelRegistry(websocket)

    async def list_entities(self) -> List[Dict[str, Any]]:
        msg_id = await self.ws._send_message({"type": "config/entity_registry/list"})

        response = await self.ws._receive_message()
        while response.get("id") != msg_id:
            response = await self.ws._receive_message()

        if not response.get("success"):
            raise Exception(f"Failed to list entities: {response}")

        self.entities = {e["entity_id"]: e for e in response.get("result", [])}
        return response.get("result", [])

    async def update_entity(
        self,
        entity_id: str,
        new_entity_id: Optional[str] = None,
        name: Optional[str] = None,
        labels: Optional[List[str]] = None,
        disabled_by: Optional[str] = None,
        enable: bool = False,
        provenance: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        message = {"type": "config/entity_registry/update", "entity_id": entity_id}

        if new_entity_id:
            message["new_entity_id"] = new_entity_id
        if name is not None:
            # Home Assistant clears a name override on ``null``; an empty
            # string would be stored verbatim and keep shadowing original_name.
            message["name"] = name or None
        if labels is not None:
            message["labels"] = labels
        if enable:
            # To enable an entity, we need to explicitly set disabled_by to None
            message["disabled_by"] = None
        elif disabled_by is not None:
            message["disabled_by"] = disabled_by

        # Log the message we're sending for debugging
        logger.info(f"Sending entity update message: {message}")

        msg_id = await self.ws._send_message(message)

        response = await self.ws._receive_message()
        while response.get("id") != msg_id:
            response = await self.ws._receive_message()

        if not response.get("success"):
            raise Exception(f"Failed to update entity {entity_id}: {response}")

        result = response.get("result", {})
        stored = await self._read_back(result, new_entity_id or entity_id)
        # Whether the registry confirmed this, not just accepted it. A write
        # that could not be read back still stands - the name is there - but
        # nothing may call it certain afterwards.
        result = {**result, "verified": stored is not None}
        if stored is not None:
            result["entity_entry"] = stored
            self._check_what_was_stored(stored, new_entity_id or entity_id, name)
        if name is not None:
            self._note_applied_name(result, entity_id, new_entity_id, name, provenance)
            await self._carry_the_title(entity_id, name)
        return result

    async def _entry_of(self, entity_id: str) -> Dict[str, Any]:
        """One registry entry, for a path that never listed them all."""
        try:
            msg_id = await self.ws._send_message({"type": "config/entity_registry/get", "entity_id": entity_id})
            answer = await self.ws._receive_message()
            while answer.get("id") != msg_id:
                answer = await self.ws._receive_message()
        except Exception as error:  # noqa: BLE001 - the rename itself stands
            logger.debug("Could not read %s: %s", entity_id, error)
            return {}
        result = answer.get("result") if answer.get("success") else None
        return result if isinstance(result, dict) else {}

    async def _carry_the_title(self, entity_id: str, name: str) -> None:
        """Write the new name into the title a helper is named by.

        Part of the rename, not follow-up work: a helper built in the interface
        reads its entity name from the title of its config entry, so leaving
        that behind means the integration keeps supplying yesterday's name and
        every later proposal is built from it.

        Only where the title is what names this entity, which is read rather
        than assumed - see supplied_names. Never fatal: the entity is renamed
        either way, and what is left is an offer to bring the title up to date.
        """
        if self.supplied_names is None:
            return
        # The device swap builds its own registry and never lists the entities,
        # so the one being renamed is fetched where it is not already known.
        known = self.entities.get(entity_id)
        entity = known or await self._entry_of(entity_id)
        entry_id = (entity or {}).get("config_entry_id")
        if not entry_id:
            return
        try:
            entries = await self.supplied_names.entries()
            siblings = (
                sum(1 for one in self.entities.values() if one.get("config_entry_id") == entry_id)
                if known
                else await self._how_many_share(entry_id)
            )
            if not supplied_names_module.is_correctable(entity, entries.get(entry_id), siblings):
                return
            await self.supplied_names.write_title(self.ws, entry_id, name)
            await self.supplied_names.reload(entry_id)
            logger.info("Carried the name %r into the title of %s", name, entry_id)
        except Exception as error:  # noqa: BLE001 - the rename itself stands
            logger.warning("Could not carry the name into the title of %s: %s", entry_id, error)

    async def _how_many_share(self, entry_id: str) -> int:
        """How many entities one config entry holds, for a path that listed none.

        A title only names a single entity when the entry holds exactly that
        one, so the count has to come from the registry where this add-on has
        not read it already.
        """
        try:
            msg_id = await self.ws._send_message({"type": "config/entity_registry/list"})
            answer = await self.ws._receive_message()
            while answer.get("id") != msg_id:
                answer = await self.ws._receive_message()
        except Exception as error:  # noqa: BLE001 - the rename itself stands
            logger.debug("Could not count what %s holds: %s", entry_id, error)
            return 0
        entries = answer.get("result") if answer.get("success") else None
        if not isinstance(entries, list):
            return 0
        return sum(1 for one in entries if isinstance(one, dict) and one.get("config_entry_id") == entry_id)

    async def _read_back(self, result: Dict[str, Any], wanted_id: str) -> Optional[Dict[str, Any]]:
        """Ask Home Assistant what it actually stored, after saying it stored it.

        A write that is acknowledged is not a write that landed as asked. Home
        Assistant numbers an entity_id that collides, cuts a name that is too
        long, and stores whatever string it is handed - including one that was
        mangled on the way in. Reading the entry back is the only way the add-on
        learns any of that; without it the list shows what was requested and the
        registry holds something else, which is how "Calc'n'Clean in 5 Tassen"
        could sit in the interface while the registry said
        "Calc&#x27;n&#x27;Clean in 5 Tassen" until the next reload.
        """
        entry = result.get("entity_entry") if isinstance(result, dict) else None
        entity_id = (entry or {}).get("entity_id") or wanted_id
        try:
            msg_id = await self.ws._send_message({"type": "config/entity_registry/get", "entity_id": entity_id})
            response = await self.ws._receive_message()
            while response.get("id") != msg_id:
                response = await self.ws._receive_message()
        except Exception as error:  # noqa: BLE001 - the write already happened
            logger.warning("Could not read %s back: %s", entity_id, error)
            return None
        if not response.get("success"):
            logger.warning("Could not read %s back: %s", entity_id, response.get("error"))
            return None
        stored = response.get("result")
        if not isinstance(stored, dict):
            return None
        # A get answers with the entry; an update wraps it. Take either.
        inner = stored.get("entity_entry")
        return inner if isinstance(inner, dict) else stored

    def _check_what_was_stored(self, stored: Dict[str, Any], wanted_id: str, wanted_name: Optional[str]) -> None:
        """Refuse to call a rename done when the registry says something else."""
        differences = []
        got_id = stored.get("entity_id")
        if wanted_id and got_id and got_id != wanted_id:
            differences.append(f"id {wanted_id!r} -> {got_id!r}")
        if wanted_name is not None:
            got_name = stored.get("name")
            # An empty name clears the override, and Home Assistant answers with
            # None for it - that is the write landing, not a difference.
            if (wanted_name or None) != got_name:
                differences.append(f"name {(wanted_name or None)!r} -> {got_name!r}")
        if differences:
            raise Exception("Home Assistant stored something else for " + wanted_id + ": " + ", ".join(differences))

    def _note_applied_name(
        self,
        result: Dict[str, Any],
        entity_id: str,
        new_entity_id: Optional[str],
        name: Optional[str],
        provenance: Optional[Dict[str, Any]],
    ) -> None:
        """Remember that this name came from here, so a later read can tell.

        Home Assistant answers a successful update with the whole entry, which
        carries the registry id - the one identifier that survives a change of
        entity_id. Failing to note it must never fail the rename itself: the
        name is already written, and a missing note only costs provenance.
        """
        if self.naming_state is None:
            return
        try:
            entry = result.get("entity_entry") if isinstance(result, dict) else None
            entry = entry if isinstance(entry, dict) else (result if isinstance(result, dict) else {})
            registry_id = entry.get("id") or (self.entities.get(entity_id, {}) or {}).get("id")
            if not registry_id:
                logger.debug("No registry id for %s, not noting who named it", entity_id)
                return
            if not name:
                # An empty name clears the override, so the integration's own
                # name applies again and there is nothing of ours left to own.
                self.naming_state.forget(registry_id)
                return
            details = dict(provenance or {})
            self.naming_state.record(
                registry_id,
                applied_name=name or "",
                applied_entity_id=new_entity_id or entity_id,
                base_entity=details.get("base_entity", ""),
                template_hash=details.get("template_hash", ""),
                won_by=details.get("won_by", "") or "",
                rule_id=details.get("rule_id"),
            )
        except Exception as error:  # noqa: BLE001 - provenance must not break renames
            logger.warning("Could not note the applied name for %s: %s", entity_id, error)

    async def rename_entity(
        self,
        old_entity_id: str,
        new_entity_id: str,
        friendly_name: Optional[str] = None,
        enable: bool = False,
        provenance: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        result = await self.update_entity(
            entity_id=old_entity_id,
            new_entity_id=new_entity_id,
            name=friendly_name,
            enable=enable,
            provenance=provenance,
        )

        # Record the successful rename in the audit log so external consumers can
        # resolve a vanished entity_id to its new one. Never let logging failures
        # break the rename itself.
        if self.rename_log is not None and new_entity_id and new_entity_id != old_entity_id:
            try:
                self.rename_log.record(old_entity_id, new_entity_id, friendly_name)
            except Exception as error:  # noqa: BLE001 - audit log must not break renames
                logger.warning("Failed to record rename in audit log: %s", error)

        return result

    async def add_labels(self, entity_id: str, labels: List[str]) -> Dict[str, Any]:
        # Stelle sicher, dass alle Labels existieren
        for label in labels:
            if label and label.strip():
                await self.label_registry.ensure_label_exists(label)

        # Hole aktuelle Entity-Informationen direkt von Home Assistant
        try:
            msg_id = await self.ws._send_message({"type": "config/entity_registry/get", "entity_id": entity_id})

            response = await self.ws._receive_message()
            while response.get("id") != msg_id:
                response = await self.ws._receive_message()

            if response.get("success"):
                entity = response.get("result", {})
                existing_labels = entity.get("labels", [])
                # Filtere leere Labels heraus
                existing_labels = [label for label in existing_labels if label and label.strip()]
                # Füge zu existierenden Labels hinzu
                new_labels = list(set(existing_labels + labels))
                # Nochmal filtern um sicherzustellen
                new_labels = [label for label in new_labels if label and label.strip()]
            else:
                # Wenn Entity nicht gefunden, setze Labels trotzdem
                logger.warning(f"Could not get entity {entity_id}, setting labels directly")
                new_labels = [label for label in labels if label and label.strip()]

        except Exception as e:
            logger.warning(f"Error getting entity {entity_id}: {e}, setting labels directly")
            new_labels = [label for label in labels if label and label.strip()]

        return await self.update_entity(entity_id=entity_id, labels=new_labels)

    async def enable_entity(self, entity_id: str) -> Dict[str, Any]:
        return await self.update_entity(entity_id=entity_id, enable=True)

    async def remove_entity(self, entity_id: str) -> Dict[str, Any]:
        """Remove an entity from the registry (for orphaned entities)."""
        message = {"type": "config/entity_registry/remove", "entity_id": entity_id}
        logger.info(f"Removing entity: {entity_id}")

        msg_id = await self.ws._send_message(message)

        response = await self.ws._receive_message()
        while response.get("id") != msg_id:
            response = await self.ws._receive_message()

        if not response.get("success"):
            raise Exception(f"Failed to remove entity {entity_id}: {response}")

        return {"success": True, "entity_id": entity_id}

    def get_disabled_entities(self) -> List[Dict[str, Any]]:
        return [entity for entity in self.entities.values() if entity.get("disabled_by") is not None]

    def get_entities_by_domain(self, domain: str) -> List[Dict[str, Any]]:
        return [entity for entity_id, entity in self.entities.items() if entity_id.startswith(f"{domain}.")]

    def get_entities_by_room(self, room: str) -> List[Dict[str, Any]]:
        return [entity for entity_id, entity in self.entities.items() if f".{room}_" in entity_id]

    def get_entities_with_label(self, label: str) -> List[Dict[str, Any]]:
        return [entity for entity in self.entities.values() if label in entity.get("labels", [])]

    def get_entities_without_label(self, label: str) -> List[Dict[str, Any]]:
        return [entity for entity in self.entities.values() if label not in entity.get("labels", [])]


class DeviceRegistry:
    def __init__(self, websocket: HomeAssistantWebSocket):
        self.ws = websocket
        self.devices: Dict[str, Dict] = {}

    async def list_devices(self) -> List[Dict[str, Any]]:
        msg_id = await self.ws._send_message({"type": "config/device_registry/list"})

        response = await self.ws._receive_message()
        while response.get("id") != msg_id:
            response = await self.ws._receive_message()

        if not response.get("success"):
            raise Exception(f"Failed to list devices: {response}")

        self.devices = {d["id"]: d for d in response.get("result", [])}
        return response.get("result", [])

    async def update_device(self, device_id: str, labels: List[str]) -> Dict[str, Any]:
        message = {
            "type": "config/device_registry/update",
            "device_id": device_id,
            "labels": labels,
        }

        msg_id = await self.ws._send_message(message)

        response = await self.ws._receive_message()
        while response.get("id") != msg_id:
            response = await self.ws._receive_message()

        if not response.get("success"):
            raise Exception(f"Failed to update device {device_id}: {response}")

        return response.get("result", {})

    def get_device_entities(self, device_id: str, entities: List[Dict]) -> List[Dict]:
        return [entity for entity in entities if entity.get("device_id") == device_id]
