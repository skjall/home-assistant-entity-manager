#!/usr/bin/env python3
"""
Device Registry - Verwaltet Geräte in Home Assistant
"""

import logging
from typing import Any, Dict, Optional

from ha_websocket import HomeAssistantWebSocket

logger = logging.getLogger(__name__)


class DeviceRegistry:
    def __init__(self, websocket: HomeAssistantWebSocket):
        self.ws = websocket

    async def rename_device(self, device_id: str, new_name: str) -> Dict[str, Any]:
        """Benennt ein Gerät um"""
        logger.info(f"Renaming device {device_id} to '{new_name}'")

        try:
            # Update device registry
            msg_id = await self.ws._send_message(
                {
                    "type": "config/device_registry/update",
                    "device_id": device_id,
                    "name_by_user": new_name,
                }
            )

            response = await self.ws._receive_message()
            while response.get("id") != msg_id:
                response = await self.ws._receive_message()

            if not response.get("success"):
                error_msg = response.get("error", {}).get("message", "Unknown error")
                logger.error(f"Failed to rename device: {error_msg}")
                raise Exception(f"Failed to rename device: {error_msg}")

            logger.info(f"Successfully renamed device {device_id}")
            return {"success": True, "device_id": device_id, "new_name": new_name}

        except Exception as e:
            logger.error(f"Error renaming device {device_id}: {str(e)}")
            raise

    async def enable_device(self, device_id: str) -> Dict[str, Any]:
        """Enable a disabled device"""
        logger.info(f"Enabling device {device_id}")

        try:
            msg_id = await self.ws._send_message(
                {
                    "type": "config/device_registry/update",
                    "device_id": device_id,
                    "disabled_by": None,
                }
            )

            response = await self.ws._receive_message()
            while response.get("id") != msg_id:
                response = await self.ws._receive_message()

            if not response.get("success"):
                error_msg = response.get("error", {}).get("message", "Unknown error")
                logger.error(f"Failed to enable device: {error_msg}")
                raise Exception(f"Failed to enable device: {error_msg}")

            logger.info(f"Successfully enabled device {device_id}")
            return {"success": True, "device_id": device_id}

        except Exception as e:
            logger.error(f"Error enabling device {device_id}: {str(e)}")
            raise

    async def assign_area(self, device_id: str, area_id: Optional[str]) -> Dict[str, Any]:
        """Assign a device to an area (or remove area assignment if area_id is None)"""
        logger.info(f"Assigning device {device_id} to area {area_id}")

        try:
            msg_id = await self.ws._send_message(
                {
                    "type": "config/device_registry/update",
                    "device_id": device_id,
                    "area_id": area_id,
                }
            )

            response = await self.ws._receive_message()
            while response.get("id") != msg_id:
                response = await self.ws._receive_message()

            if not response.get("success"):
                error_msg = response.get("error", {}).get("message", "Unknown error")
                logger.error(f"Failed to assign area: {error_msg}")
                raise Exception(f"Failed to assign area: {error_msg}")

            logger.info(f"Successfully assigned device {device_id} to area {area_id}")
            return {"success": True, "device_id": device_id, "area_id": area_id}

        except Exception as e:
            logger.error(f"Error assigning device {device_id} to area: {str(e)}")
            raise

    async def disable_device(self, device_id: str) -> Dict[str, Any]:
        """Deaktiviert ein Gerät (disabled_by='user')."""
        logger.info(f"Disabling device {device_id}")

        try:
            msg_id = await self.ws._send_message(
                {
                    "type": "config/device_registry/update",
                    "device_id": device_id,
                    "disabled_by": "user",
                }
            )

            response = await self.ws._receive_message()
            while response.get("id") != msg_id:
                response = await self.ws._receive_message()

            if not response.get("success"):
                error_msg = response.get("error", {}).get("message", "Unknown error")
                logger.error(f"Failed to disable device: {error_msg}")
                raise Exception(f"Failed to disable device: {error_msg}")

            logger.info(f"Successfully disabled device {device_id}")
            return {"success": True, "device_id": device_id}

        except Exception as e:
            logger.error(f"Error disabling device {device_id}: {str(e)}")
            raise

    async def remove_config_entry(self, device_id: str, config_entry_id: str) -> Dict[str, Any]:
        """Entfernt ein Gerät über einen seiner Config-Entries.

        Dies ist derselbe Pfad, den auch das HA-Frontend beim "Gerät löschen"
        nutzt; für Integrationen wie Matter/Z2M (MQTT) löst HA dabei das
        eigentliche Entfernen aus der Integration aus.

        Manche Integrationen lehnen das Entfernen ab (z.B. Matter, wenn das
        Gerät noch erreichbar ist) - dann wirft diese Methode eine Exception
        mit der HA-Fehlermeldung, die der Aufrufer als Teilerfolg behandeln kann.
        """
        logger.info(f"Removing config_entry {config_entry_id} from device {device_id}")

        try:
            msg_id = await self.ws._send_message(
                {
                    "type": "config/device_registry/remove_config_entry",
                    "device_id": device_id,
                    "config_entry_id": config_entry_id,
                }
            )

            response = await self.ws._receive_message()
            while response.get("id") != msg_id:
                response = await self.ws._receive_message()

            if not response.get("success"):
                error_msg = response.get("error", {}).get("message", "Unknown error")
                logger.error(f"Failed to remove config_entry from device: {error_msg}")
                raise Exception(f"Failed to remove device: {error_msg}")

            # result is the updated device (None/empty if the device was fully removed)
            logger.info(f"Successfully removed config_entry {config_entry_id} from device {device_id}")
            return {"success": True, "device_id": device_id, "result": response.get("result")}

        except Exception as e:
            logger.error(f"Error removing config_entry from device {device_id}: {str(e)}")
            raise

    async def get_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Holt Geräteinformationen"""
        try:
            msg_id = await self.ws._send_message({"type": "config/device_registry/list"})

            response = await self.ws._receive_message()
            while response.get("id") != msg_id:
                response = await self.ws._receive_message()

            if response.get("success"):
                devices = response.get("result", [])
                for device in devices:
                    if device.get("id") == device_id:
                        return device

            return None

        except Exception as e:
            logger.error(f"Error getting device {device_id}: {str(e)}")
            return None
