"""The name an integration supplies, where it is ours to correct.

Most supplied names come out of an integration's code or its translation files
and cannot be touched from here. One kind can: a helper built in the Home
Assistant interface - a Riemann sum, a template sensor, a group - takes its
entity name from the title of its config entry, and that title is writable.

Such a name freezes on the day the helper is created. Move the socket behind it
from one room to another and the helper still says "Kammer Lüftungsanlage
Steckdose Energie", so every proposal built from it carries a room the entity
left long ago.

Three things have to hold before this module offers to correct one, and each of
them is checked rather than guessed:

* the supplied name is word for word the title of the entity's config entry,
  which is what proves the name comes from the title and not from code,
* the entity is the only one that entry has, so one title means one name,
* the entity is called something else today.
"""

import logging
from typing import Any, Dict, List, Optional

import aiohttp

from naming_canon import canon

logger = logging.getLogger(__name__)


def is_correctable(entity: Dict[str, Any], entry: Optional[Dict[str, Any]], siblings: int) -> bool:
    """Whether this entity's supplied name is the title of its own entry."""
    if not entry or siblings != 1:
        return False
    supplied = entity.get("original_name") or ""
    if not supplied:
        return False
    return canon(supplied) == canon(entry.get("title") or "")


def what_to_correct(entity: Dict[str, Any], entry: Optional[Dict[str, Any]], siblings: int) -> Optional[Dict[str, Any]]:
    """The correction to offer for this entity, or nothing.

    Offered only where the entity already carries another name: that name is
    what the title should have said, so nothing has to be invented.
    """
    if not is_correctable(entity, entry, siblings):
        return None
    written = entity.get("name") or ""
    supplied = entity.get("original_name") or ""
    if not written or canon(written) == canon(supplied):
        return None
    return {
        "entry_id": entity.get("config_entry_id"),
        "domain": (entry or {}).get("domain"),
        "supplied": supplied,
        "title_should_be": written,
    }


class SuppliedNames:
    """Reads config entries and writes the title one of them is named by."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def entries(self) -> Dict[str, Dict[str, Any]]:
        """Every config entry, by id."""
        url = f"{self.base_url}/api/config/config_entries/entry"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                response.raise_for_status()
                rows: List[Dict[str, Any]] = await response.json()
        return {row["entry_id"]: row for row in rows if row.get("entry_id")}

    @staticmethod
    async def write_title(websocket: Any, entry_id: str, title: str) -> Dict[str, Any]:
        """Set the title the entity is named after, and say what came back."""
        message = {"type": "config_entries/update", "entry_id": entry_id, "title": title}
        msg_id = await websocket._send_message(message)
        answer = await websocket._receive_message()
        while answer.get("id") != msg_id:
            answer = await websocket._receive_message()
        if not answer.get("success"):
            raise RuntimeError(f"Home Assistant refused to retitle {entry_id}: {answer.get('error')}")
        return answer.get("result") or {}

    async def reload(self, entry_id: str) -> bool:
        """Load the entry again, so it builds its entity name afresh."""
        url = f"{self.base_url}/api/config/config_entries/entry/{entry_id}/reload"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers) as response:
                if response.status != 200:
                    logger.warning("Could not reload %s: %s", entry_id, response.status)
                    return False
                answer = await response.json()
        # A restart is asked for when the entry cannot be unloaded; the title is
        # written either way, it just does not show until then.
        return not answer.get("require_restart", False)
