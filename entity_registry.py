import logging
from typing import Any, Dict, List, Optional

from ha_websocket import HomeAssistantWebSocket
from label_registry import LabelRegistry

logger = logging.getLogger(__name__)

# Sentinel to distinguish "caller did not pass this field" from "caller explicitly
# passed None" (which is a meaningful value for area_id/hidden_by/etc. - it clears
# the override / restores the default).
_UNSET = object()


class EntityRegistry:
    # Optional shared audit log for entity_id renames. Set once by the web app
    # at startup (see web_ui.py); stays None in contexts that don't wire it up.
    rename_log = None

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
        icon: Any = _UNSET,
        area_id: Any = _UNSET,
        hidden_by: Any = _UNSET,
        entity_category: Any = _UNSET,
        device_class: Any = _UNSET,
    ) -> Dict[str, Any]:
        """Update an entity registry entry.

        Home Assistant's ``config/entity_registry/update`` command accepts the user
        overrides icon, area_id, hidden_by, entity_category and device_class in
        addition to name/new_entity_id/labels/disabled_by. For the override fields a
        sentinel (``_UNSET``) is used so that an explicit ``None`` (which clears the
        override / restores the default) can be told apart from "leave untouched".
        """
        message = {"type": "config/entity_registry/update", "entity_id": entity_id}

        if new_entity_id:
            message["new_entity_id"] = new_entity_id
        if name:
            message["name"] = name
        if labels is not None:
            message["labels"] = labels
        if enable:
            # To enable an entity, we need to explicitly set disabled_by to None
            message["disabled_by"] = None
        elif disabled_by is not None:
            message["disabled_by"] = disabled_by
        if icon is not _UNSET:
            message["icon"] = icon
        if area_id is not _UNSET:
            message["area_id"] = area_id
        if hidden_by is not _UNSET:
            message["hidden_by"] = hidden_by
        if entity_category is not _UNSET:
            message["entity_category"] = entity_category
        if device_class is not _UNSET:
            message["device_class"] = device_class

        # Log the message we're sending for debugging
        logger.info(f"Sending entity update message: {message}")

        msg_id = await self.ws._send_message(message)

        response = await self.ws._receive_message()
        while response.get("id") != msg_id:
            response = await self.ws._receive_message()

        if not response.get("success"):
            raise Exception(f"Failed to update entity {entity_id}: {response}")

        return response.get("result", {})

    async def get_entity(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """Fetch the extended registry entry for a single entity.

        Unlike ``config/entity_registry/list`` (partial entries), the ``get`` command
        returns the full entry including the user ``device_class`` override, which is
        needed when copying an entity's settings during a device swap. Returns None if
        the entity is unknown.
        """
        msg_id = await self.ws._send_message({"type": "config/entity_registry/get", "entity_id": entity_id})

        response = await self.ws._receive_message()
        while response.get("id") != msg_id:
            response = await self.ws._receive_message()

        if not response.get("success"):
            logger.warning(f"Could not get entity {entity_id}: {response}")
            return None

        return response.get("result", {})

    async def rename_entity(
        self,
        old_entity_id: str,
        new_entity_id: str,
        friendly_name: Optional[str] = None,
        enable: bool = False,
    ) -> Dict[str, Any]:
        result = await self.update_entity(
            entity_id=old_entity_id, new_entity_id=new_entity_id, name=friendly_name, enable=enable
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
