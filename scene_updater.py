#!/usr/bin/env python3
"""
Scene Updater - Aktualisiert Entity IDs in Scenes über die REST API
"""

import asyncio
import logging
import os

import aiohttp
from dotenv import load_dotenv

from ha_client import HomeAssistantClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()


class SceneUpdater:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def get_scene_config(self, scene_id: str) -> dict:
        """Hole die Konfiguration einer Scene"""
        # Scene ID Format: scene.name -> ID extrahieren
        async with aiohttp.ClientSession() as session:
            # Erst alle Scenes holen um die numerische ID zu finden
            states_url = f"{self.base_url}/api/states"
            async with session.get(states_url, headers=self.headers) as response:
                states = await response.json()

            # Finde die Scene
            scene_numeric_id = None
            for state in states:
                if state["entity_id"] == scene_id:
                    # Die numerische ID ist im 'id' Attribut
                    scene_numeric_id = state.get("attributes", {}).get("id")
                    break

            if not scene_numeric_id:
                logger.error(f"Scene {scene_id} nicht gefunden oder hat keine ID")
                return None

            # Hole Scene Config
            url = f"{self.base_url}/api/config/scene/config/{scene_numeric_id}"
            async with session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.error(f"Fehler beim Abrufen der Scene {scene_id}: {response.status}")
                    return None

    async def update_scene_config(self, scene_numeric_id: str, config: dict) -> bool:
        """Aktualisiere eine Scene Konfiguration"""
        url = f"{self.base_url}/api/config/scene/config/{scene_numeric_id}"

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=config) as response:
                if response.status == 200:
                    result = await response.json()
                    return result.get("result") == "ok"
                else:
                    logger.error(f"Fehler beim Update der Scene: {response.status}")
                    return False

    async def update_entity_in_scene(self, scene_id: str, old_entity_id: str, new_entity_id: str) -> bool:
        """Ersetze eine Entity ID in einer Scene"""
        # Hole aktuelle Config
        config = await self.get_scene_config(scene_id)
        if not config:
            return False

        scene_numeric_id = config["id"]

        # Prüfe ob die alte Entity in der Scene ist
        if old_entity_id not in config.get("entities", {}):
            logger.info(f"Entity {old_entity_id} nicht in Scene {scene_id} gefunden")
            return False

        # Kopiere die Entity Config zur neuen ID
        entity_config = config["entities"].pop(old_entity_id)
        config["entities"][new_entity_id] = entity_config

        logger.info(f"Aktualisiere Scene {scene_id}: {old_entity_id} -> {new_entity_id}")

        # Update die Scene
        return await self.update_scene_config(scene_numeric_id, config)

    async def update_entity_in_all_scenes(self, old_entity_id: str, new_entity_id: str) -> dict:
        """Aktualisiere eine Entity ID in allen Scenes"""
        client = HomeAssistantClient(self.base_url, self.token)

        results = {"success": [], "failed": [], "skipped": []}

        async with client:
            states = await client.get_states()

            # Finde alle Scenes die die alte Entity verwenden
            for state in states:
                if state["entity_id"].startswith("scene."):
                    entity_ids = state.get("attributes", {}).get("entity_id", [])

                    if old_entity_id in entity_ids:
                        # Diese Scene muss aktualisiert werden
                        success = await self.update_entity_in_scene(state["entity_id"], old_entity_id, new_entity_id)

                        if success:
                            results["success"].append(state["entity_id"])
                        else:
                            results["failed"].append(state["entity_id"])

        return results


async def main():
    base_url = os.getenv("HA_URL")
    token = os.getenv("HA_TOKEN")

    updater = SceneUpdater(base_url, token)

    # Test: Ersetze alte Entity ID durch neue
    old_entity = "light.buro_bucherregal_spots"
    new_entity = "light.buro_bucherregal_spots_licht"

    print(f"Aktualisiere {old_entity} -> {new_entity} in allen Scenes...")

    results = await updater.update_entity_in_all_scenes(old_entity, new_entity)

    print("\nErgebnisse:")
    print(f"Erfolgreich aktualisiert: {len(results['success'])}")
    for scene in results["success"]:
        print(f"  ✓ {scene}")

    if results["failed"]:
        print(f"\nFehlgeschlagen: {len(results['failed'])}")
        for scene in results["failed"]:
            print(f"  ✗ {scene}")


if __name__ == "__main__":
    asyncio.run(main())
