"""Getting the registries in front of whoever needs them.

Reading the entity and device registries is cheap; building the hierarchy and
the rename proposals on top of them is not. Anything that only needs to know
what exists asks here and gets the cheap half, loaded once.
"""

import logging
import os

from app_state import ha_translations, init_client, renamer_state
from ha_websocket import HomeAssistantWebSocket
from json_store import new_lock

logger = logging.getLogger(__name__)

# Reading the registries takes a socket and a second; only one caller does it.
_loading = new_lock()


async def sync_ha_language(ws) -> None:
    """Follow Home Assistant's language and load the names it uses.

    Home Assistant is the one place a user sets their language, and its own
    translations cover far more languages than this add-on could maintain.
    """
    rules = renamer_state["naming_rules"]
    try:
        config = await ws.get_config()
        language = (config.get("language") or "").split("-")[0]
    except Exception as error:
        logger.warning("Could not read the Home Assistant language: %s", error)
        language = rules.language
    if language and language != rules.language:
        logger.info("Following the Home Assistant language: %s", language)
        rules.set_language(language)
        renamer_state["type_mappings"]._refresh_user_view()
    integrations = {
        entity.get("platform") for entity in renamer_state["restructurer"].entities.values() if entity.get("platform")
    }
    await ha_translations.load(ws, rules.language, integrations)
    # The English names of the device classes say which supplied names mean the
    # same thing as their class; one call, no integrations needed.
    if rules.language != "en":
        await ha_translations.load(ws, "en")


async def ensure_registry_loaded() -> None:
    """Make sure the entity list is there, without the cost of a full hierarchy.

    Reading the registries takes about a tenth of a second; the states and the
    rename suggestions that /api/hierarchy also builds take ten times that and
    say nothing about how far a rule reaches.
    """
    restructurer = renamer_state.get("restructurer")
    if restructurer is None:
        await init_client()
        restructurer = renamer_state["restructurer"]
    if restructurer.entities:
        return
    # Whoever arrives while the first caller is still reading waits here and
    # then finds the entities already there; without this every one of them
    # would open its own socket and read the whole registry again.
    with _loading:
        if restructurer.entities:
            return
        await _load_registry(restructurer)


async def _load_registry(restructurer) -> None:
    """Reads the registries once. Runs only under _loading."""
    token = os.getenv("HA_TOKEN", os.getenv("SUPERVISOR_TOKEN"))
    base_url = os.getenv("HA_URL")
    ws_url = (
        base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
        if base_url
        else "ws://supervisor/core/websocket"
    )
    ws = HomeAssistantWebSocket(ws_url, token)
    try:
        await ws.connect()
        await restructurer.load_structure(ws)
        await sync_ha_language(ws)
    except Exception as error:
        logger.warning("Could not load the registries: %s", error)
    finally:
        try:
            await ws.disconnect()
        except Exception:
            pass
