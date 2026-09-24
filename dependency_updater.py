#!/usr/bin/env python3
"""
Dependency Updater - Aktualisiert Entity IDs in Scenes, Scripts und Automations
"""

import asyncio
import copy
import logging
import os
from typing import Any, Dict, List, Optional

import aiohttp
from dotenv import load_dotenv

from entity_ref_utils import refers_to_entity, replace_entity_in_obj
from helper_options import HelperOptions

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()


class DependencyUpdater:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        # Helpers built in the interface keep their entity ids in free text,
        # which no rename reaches on its own. Shared across one job so the
        # options are read once, not once per entity.
        self.helpers = HelperOptions(self.base_url, self.token)
        # Every automation has to be read to find out whether it names the
        # entity being renamed, and the config API only hands them out one at
        # a time. Read once per updater - which is once per job - and kept
        # current here, rather than read again for every entity: an
        # installation with a hundred automations spent three seconds per
        # entity on nothing but these reads.
        self._automation_configs: Optional[Dict[str, Optional[Dict]]] = None
        self._configs_lock = asyncio.Lock()

    async def get_states(self) -> List[Dict]:
        """Hole alle States"""
        async with aiohttp.ClientSession() as session:
            url = f"{self.base_url}/api/states"
            async with session.get(url, headers=self.headers) as response:
                return await response.json()

    # ===== SCENES =====
    async def get_scene_config(self, scene_numeric_id: str) -> Optional[Dict]:
        """Hole Scene Konfiguration"""
        url = f"{self.base_url}/api/config/scene/config/{scene_numeric_id}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.error(f"Fehler beim Abrufen der Scene {scene_numeric_id}: {response.status}")
                    return None

    async def update_scene_config(self, scene_numeric_id: str, config: Dict) -> bool:
        """Aktualisiere Scene Konfiguration"""
        url = f"{self.base_url}/api/config/scene/config/{scene_numeric_id}"

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=config) as response:
                if response.status == 200:
                    result = await response.json()
                    return result.get("result") == "ok"
                else:
                    logger.error(f"Fehler beim Update der Scene: {response.status}")
                    return False

    async def update_scene_entities(
        self,
        scene_id: str,
        scene_numeric_id: str,
        old_entity_id: str,
        new_entity_id: str,
    ) -> bool:
        """Aktualisiere Entity in einer Scene"""
        config = await self.get_scene_config(scene_numeric_id)
        if not config:
            return False

        # Prüfe ob die alte Entity in der Scene ist
        if old_entity_id not in config.get("entities", {}):
            return False

        # Ersetze die Entity
        entity_config = config["entities"].pop(old_entity_id)
        config["entities"][new_entity_id] = entity_config

        logger.info(f"Aktualisiere Scene {scene_id}: {old_entity_id} -> {new_entity_id}")
        return await self.update_scene_config(scene_numeric_id, config)

    # ===== SCRIPTS =====
    async def get_script_config(self, script_id: str) -> Optional[Dict]:
        """Hole Script Konfiguration"""
        # script.name -> name extrahieren
        script_name = script_id.replace("script.", "")
        url = f"{self.base_url}/api/config/script/config/{script_name}"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.error(f"Fehler beim Abrufen des Scripts {script_id}: {response.status}")
                    return None

    async def update_script_config(self, script_id: str, config: Dict) -> bool:
        """Aktualisiere Script Konfiguration"""
        script_name = script_id.replace("script.", "")
        url = f"{self.base_url}/api/config/script/config/{script_name}"

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=config) as response:
                if response.status == 200:
                    result = await response.json()
                    return result.get("result") == "ok"
                else:
                    logger.error(f"Fehler beim Update des Scripts: {response.status}")
                    return False

    def replace_entity_in_dict(self, data: Any, old_entity_id: str, new_entity_id: str) -> bool:
        """Rekursiv Entity IDs in einem Dictionary ersetzen (in-place).

        Unterstützt:
        - entity_id: "entity.id" (direkter Wert)
        - entity_id: ["entity.id", ...] (Liste unter entity_id Key)
        - beliebiger_key: "entity.id" (z.B. Blueprint-Inputs)
        - beliebiger_key: ["entity.id", ...] (z.B. Blueprint-Input Listen)
        - Templates mit {{ entity.id }} (wortgrenzen-sicher)

        Delegiert an die zentrale Logik in entity_ref_utils, damit Scanner,
        Updater und Lovelace-Updater dieselbe Ersetzung verwenden.
        """
        return replace_entity_in_obj(data, old_entity_id, new_entity_id)

    async def update_script_entities(self, script_id: str, old_entity_id: str, new_entity_id: str) -> bool:
        """Aktualisiere Entity in einem Script"""
        config = await self.get_script_config(script_id)
        if not config:
            return False

        # Ersetze Entity IDs rekursiv
        changed = self.replace_entity_in_dict(config, old_entity_id, new_entity_id)

        if changed:
            logger.info(f"Aktualisiere Script {script_id}: {old_entity_id} -> {new_entity_id}")
            return await self.update_script_config(script_id, config)

        return False

    # ===== AUTOMATIONS =====
    async def fetch_automation_config(
        self, automation_numeric_id: str, session: Optional[aiohttp.ClientSession] = None
    ) -> Optional[Dict]:
        """Read one automation's configuration from Home Assistant.

        The only place that asks for it over HTTP. A caller that wants what was
        already read asks ``read_automation_config``, or ``get_automation_config``
        for a copy to change; the two calls here are the ones that have to reach
        Home Assistant - filling the store, and reading a write back to see that
        it took.
        """
        url = f"{self.base_url}/api/config/automation/config/{automation_numeric_id}"

        async def read(open_session: aiohttp.ClientSession) -> Optional[Dict]:
            async with open_session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    return await response.json()
                text = await response.text()
                logger.error(f"Could not read automation {automation_numeric_id}: {response.status}, response: {text}")
                return None

        if session is not None:
            return await read(session)
        async with aiohttp.ClientSession() as own:
            return await read(own)

    async def load_automation_configs(self, automation_states: List[Dict]) -> None:
        """Read every automation's configuration once, over one connection.

        The reads are independent, so a few run at a time; more than a handful
        at once buys little and asks a lot of a Home Assistant that is also
        answering the interface.

        It fills the store for this job and answers nothing: a caller reads
        through ``read_automation_config``, which is also what answers where
        this was called with other automations than the ones being asked about.
        """
        # Under the lock: two jobs starting together both found nothing
        # here, both read every automation, and the one that finished second
        # put its own reading in place of the first - along with everything
        # the first had written back into it in the meantime.
        async with self._configs_lock:
            await self._load_automation_configs(automation_states)

    async def _load_automation_configs(self, automation_states: List[Dict]) -> None:
        if self._automation_configs is not None:
            return

        numeric_ids = []
        for state in automation_states:
            numeric_id = state.get("attributes", {}).get("id")
            if numeric_id:
                numeric_ids.append(numeric_id)

        # Only what was read is kept. A read that failed says nothing about
        # the automation - a timeout and a configuration this API cannot hand
        # out look the same from here - so it is asked for again rather than
        # answered with "there is none" for the rest of the job.
        configs: Dict[str, Optional[Dict]] = {}
        at_a_time = asyncio.Semaphore(8)

        async with aiohttp.ClientSession() as session:

            async def one(numeric_id: str) -> None:
                async with at_a_time:
                    try:
                        config = await self.fetch_automation_config(numeric_id, session)
                    except Exception as error:  # noqa: BLE001 - one unreadable automation is not the job
                        logger.error(f"Automation {numeric_id} could not be read: {error}")
                        return
                    if config is not None:
                        configs[numeric_id] = config

            await asyncio.gather(*(one(numeric_id) for numeric_id in numeric_ids))

        logger.info(f"Read {len(configs)} of {len(numeric_ids)} automation configurations once for this job")
        # Nothing at all where something was asked for is Home Assistant not
        # answering, not an installation without automations. Keeping that as
        # the reading for the job answered every later entity with nothing.
        if configs or not numeric_ids:
            self._automation_configs = configs

    async def read_automation_config(self, automation_numeric_id: str) -> Optional[Dict]:
        """The automation's configuration as it stands, for reading only.

        The caller must not write into what comes back; ``get_automation_config``
        is the one that hands out a copy to change.
        """
        if self._automation_configs is not None and automation_numeric_id in self._automation_configs:
            return self._automation_configs[automation_numeric_id]
        return await self.fetch_automation_config(automation_numeric_id)

    async def get_automation_config(self, automation_numeric_id: str) -> Optional[Dict]:
        """The automation's configuration, out of what this job has read.

        A copy, because the caller rewrites the entity ids inside it. Handing
        out what is kept would put a rename into the store before it is
        written, and a write that then fails would leave the next entity of the
        same job working from a configuration Home Assistant does not have.
        """
        config = await self.read_automation_config(automation_numeric_id)
        return copy.deepcopy(config) if config is not None else None

    async def update_automation_config(
        self,
        automation_numeric_id: str,
        config: Dict,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> bool:
        """Aktualisiere Automation Konfiguration"""
        url = f"{self.base_url}/api/config/automation/config/{automation_numeric_id}"

        async def write(open_session: aiohttp.ClientSession) -> bool:
            async with open_session.post(url, headers=self.headers, json=config) as response:
                if response.status == 200:
                    result = await response.json()
                    if result.get("result") == "ok":
                        return True
                    # A 200 that does not say "ok" used to leave no trace at
                    # all, which is the worst of both: the write did not take
                    # and nothing said why.
                    logger.error(f"Automation {automation_numeric_id} was not written: {result}")
                    return False
                else:
                    text = await response.text()
                    logger.error(f"Could not write automation {automation_numeric_id}: {response.status}, {text}")
                    return False

        if session is not None:
            return await write(session)
        async with aiohttp.ClientSession() as own:
            return await write(own)

    def names_entity(self, config: Dict, entity_id: str) -> bool:
        """Whether the configuration refers to the entity, as a reference.

        Not as text: an automation described as "watches sensor.old" says the
        name without referring to it, and a rename that correctly left the
        prose alone was then reported as one that had failed. The reading is
        the one the rewrite goes by, asked without writing and without copying
        the configuration to write into.
        """
        return refers_to_entity(config, entity_id)

    async def update_automation_entities(
        self,
        automation_id: str,
        automation_numeric_id: str,
        old_entity_id: str,
        new_entity_id: str,
        config: Optional[Dict] = None,
    ) -> bool:
        """Aktualisiere Entity in einer Automation

        ``config`` is the configuration the caller has already read. Reading it
        a second time here meant the decision to write was taken on one
        reading and the write built on another, so an automation changed in
        Home Assistant between the two was reported as a failure.
        """
        supplied = config is not None
        if config is None:
            config = await self.get_automation_config(automation_numeric_id)
        else:
            config = copy.deepcopy(config)
        if not config:
            # Said apart: a configuration that could not be read at all, and one
            # that was handed over empty. The second looked like a failed read
            # and had the reader looking for a connection that was working.
            if supplied:
                logger.error(f"Automation {automation_id} was handed over with an empty configuration")
            else:
                logger.error(f"Could not fetch config for automation {automation_id}")
            return False

        logger.debug(f"Got config, checking for entity {old_entity_id}")
        # Ersetze Entity IDs rekursiv
        changed = self.replace_entity_in_dict(config, old_entity_id, new_entity_id)

        if changed:
            logger.info(f"Updating automation {automation_id}: {old_entity_id} -> {new_entity_id}")
            # The write and the read that proves it share one connection.
            async with aiohttp.ClientSession() as session:
                if not await self.update_automation_config(automation_numeric_id, config, session):
                    # It may have been applied and then reported as an error.
                    # What is kept is only what was read back, so the entry
                    # goes rather than answering the rest of the job with a
                    # version from before a write that may have taken.
                    async with self._configs_lock:
                        if self._automation_configs is not None:
                            self._automation_configs.pop(automation_numeric_id, None)
                    return False
                # Home Assistant answering "ok" is not evidence that the old id
                # is gone: a write that reported success and changed nothing
                # looks exactly like one that worked. Reading it back is the
                # only proof, and without it a rename silently leaves an
                # automation broken.
                written = await self.fetch_automation_config(automation_numeric_id, session)
            # The next entity of this rename reads the automation again, and
            # what it has to see is the version just written, not the one from
            # before it named the new id. A read-back that failed says nothing
            # about what Home Assistant holds, so the entry is dropped rather
            # than answered with nothing for the rest of the job.
            if written is None:
                async with self._configs_lock:
                    if self._automation_configs is not None:
                        self._automation_configs.pop(automation_numeric_id, None)
                logger.error(f"Could not read automation {automation_id} back after writing it")
                return False
            if self.names_entity(written, old_entity_id):
                # Kept only where it proved out. A reading that still refers to
                # the old id says the write did not take, and storing it would
                # have the rest of the job build on a version Home Assistant
                # may never have held.
                async with self._configs_lock:
                    if self._automation_configs is not None:
                        self._automation_configs.pop(automation_numeric_id, None)
                logger.error(f"Automation {automation_id} still names {old_entity_id} after the write")
                return False
            async with self._configs_lock:
                if self._automation_configs is not None:
                    self._automation_configs[automation_numeric_id] = written
            logger.info(f"Automation {automation_id} now names {new_entity_id}")
            return True
        else:
            # Nothing was written, and the caller hears the same "no" it hears
            # for a write that failed. Said out loud, because the two are worth
            # telling apart: a rename asks names_entity first and gets here only
            # where the automation stopped naming the entity in between, while
            # repairing a broken reference (web_ui) calls this without asking and
            # gets here for a reference the walk does not recognise.
            logger.warning(f"Automation {automation_id} names no reference to {old_entity_id}; nothing was written")

        return False

    # ===== MAIN UPDATE =====
    async def update_all_dependencies(
        self, old_entity_id: str, new_entity_id: str, cached_states: Optional[List[Dict]] = None
    ) -> Dict[str, List[str]]:
        """Aktualisiere alle Dependencies"""
        logger.info("=== DependencyUpdater.update_all_dependencies called ===")
        logger.info(f"Old entity: {old_entity_id}, New entity: {new_entity_id}")

        results = {
            "scenes": {"success": [], "failed": []},
            "scripts": {"success": [], "failed": []},
            # "unreachable" is neither: the automation names the old id and
            # nothing here can rewrite it, so only the user can.
            "automations": {"success": [], "failed": [], "unreachable": []},
            "helpers": {"success": [], "failed": []},
            "total_success": 0,
            "total_failed": 0,
            "total_unreachable": 0,
            # Automations the config API cannot read at all, whether or not
            # they are about this entity. Worth saying once, not per rename.
            "unreachable_configs": [],
        }

        # Use cached states if provided, otherwise fetch
        if cached_states is not None:
            states = cached_states
            logger.info(f"Using cached states ({len(states)} states)")
        else:
            logger.info("Fetching states from Home Assistant...")
            states = await self.get_states()
            logger.info(f"Got {len(states)} states")

        # Count automations for debugging
        automation_count = sum(1 for s in states if s["entity_id"].startswith("automation."))
        logger.info(f"Found {automation_count} automations in states")

        for state in states:
            entity_id = state["entity_id"]
            attributes = state.get("attributes", {})

            # SCENES
            if entity_id.startswith("scene."):
                entity_ids = attributes.get("entity_id", [])
                if old_entity_id in entity_ids:
                    scene_numeric_id = attributes.get("id")
                    if scene_numeric_id:
                        success = await self.update_scene_entities(
                            entity_id, scene_numeric_id, old_entity_id, new_entity_id
                        )
                        if success:
                            results["scenes"]["success"].append(entity_id)
                            results["total_success"] += 1
                        else:
                            results["scenes"]["failed"].append(entity_id)
                            results["total_failed"] += 1

            # SCRIPTS
            elif entity_id.startswith("script."):
                # A reference, not a mention, and not a piece of a longer id -
                # the same question the automations are asked. Read as text, a
                # script called "Manages sensor.old" was fetched, found to name
                # nothing, and reported as a rename that had failed.
                if self.names_entity(attributes, old_entity_id):
                    success = await self.update_script_entities(entity_id, old_entity_id, new_entity_id)
                    if success:
                        results["scripts"]["success"].append(entity_id)
                        results["total_success"] += 1
                    else:
                        results["scripts"]["failed"].append(entity_id)
                        results["total_failed"] += 1

            # AUTOMATIONS
            elif entity_id.startswith("automation."):
                # Skip - we'll handle automations separately via REST API
                pass

        # Handle automations via REST API
        logger.info("Checking automations via REST API...")
        automation_states = [s for s in states if s["entity_id"].startswith("automation.")]
        logger.info(f"Found {len(automation_states)} automations to check")
        await self.load_automation_configs(automation_states)

        for automation_state in automation_states:
            automation_entity_id = automation_state["entity_id"]
            automation_numeric_id = automation_state.get("attributes", {}).get("id")

            if automation_numeric_id:
                # Read, not rewritten, so the stored configuration answers as
                # it stands; update_automation_entities takes its own copy.
                config = await self.read_automation_config(automation_numeric_id)
                if config:
                    if self.names_entity(config, old_entity_id):
                        logger.info(f"Found automation {automation_entity_id} using {old_entity_id}")
                        success = await self.update_automation_entities(
                            automation_entity_id,
                            automation_numeric_id,
                            old_entity_id,
                            new_entity_id,
                            config,
                        )
                        if success:
                            results["automations"]["success"].append(automation_entity_id)
                            results["total_success"] += 1
                        else:
                            results["automations"]["failed"].append(automation_entity_id)
                            results["total_failed"] += 1
                else:
                    # Automations outside automations.yaml - packages, or files
                    # included from elsewhere - cannot be read or written
                    # through the config API. If one of them names the old id,
                    # nothing here will ever change it, so say so rather than
                    # leaving a line in the debug log.
                    # A reference, not a mention: the state is read the same
                    # way a configuration is, so neither a friendly name that
                    # says the id in prose nor an id this one only begins -
                    # "sensor.power" in "automation.sensor.power_monitor" -
                    # counts as one this add-on cannot reach.
                    if self.names_entity(automation_state, old_entity_id):
                        results["automations"]["unreachable"].append(automation_entity_id)
                        results["total_unreachable"] += 1
                    else:
                        results["unreachable_configs"].append(automation_entity_id)
            else:
                logger.debug(f"Automation {automation_entity_id} has no numeric ID")

        # Helpers built in the interface. Their templates name entities in
        # prose, so Home Assistant carries nothing over for them.
        try:
            helpers = await self.helpers.rename(old_entity_id, new_entity_id)
            results["helpers"] = helpers
            results["total_success"] += len(helpers["success"])
            results["total_failed"] += len(helpers["failed"])
        except Exception as error:  # noqa: BLE001 - a rename must not fail over a helper
            logger.error("Could not carry the rename into the helpers: %s", error)

        return results


async def main():
    """Test des Dependency Updaters"""
    base_url = os.getenv("HA_URL")
    token = os.getenv("HA_TOKEN")

    updater = DependencyUpdater(base_url, token)

    # Test
    old_entity = "light.buro_bucherregal_spots_licht"  # Die neue ID von vorhin
    new_entity = "light.buro_bucherregal_spots"  # Zurück zur alten als Test

    print(f"Aktualisiere {old_entity} -> {new_entity} in allen Dependencies...")

    results = await updater.update_all_dependencies(old_entity, new_entity)

    print("\n" + "=" * 60)
    print("ERGEBNISSE")
    print("=" * 60)

    # Scenes
    if results["scenes"]["success"] or results["scenes"]["failed"]:
        print("\nSCENES:")
        for scene in results["scenes"]["success"]:
            print(f"  ✓ {scene}")
        for scene in results["scenes"]["failed"]:
            print(f"  ✗ {scene}")

    # Scripts
    if results["scripts"]["success"] or results["scripts"]["failed"]:
        print("\nSCRIPTS:")
        for script in results["scripts"]["success"]:
            print(f"  ✓ {script}")
        for script in results["scripts"]["failed"]:
            print(f"  ✗ {script}")

    # Automations
    if results["automations"]["success"] or results["automations"]["failed"]:
        print("\nAUTOMATIONS:")
        for auto in results["automations"]["success"]:
            print(f"  ✓ {auto}")
        for auto in results["automations"]["failed"]:
            print(f"  ✗ {auto}")

    print(f"\n{'='*60}")
    print(f"Gesamt: {results['total_success']} erfolgreich, {results['total_failed']} fehlgeschlagen")


if __name__ == "__main__":
    asyncio.run(main())
