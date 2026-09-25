"""What this add-on itself has written into the entity registry.

Home Assistant's registry says what an entity is called, but not who decided
it. Without that, every read has to guess: a name that looks like one of ours
might be one we wrote, or one somebody typed by hand in the Home Assistant
interface. Guessing costs either way - re-render a name a user chose, or leave
ours behind when the rules change.

So each successful write is noted here, keyed by the registry's own id for the
entity, which survives a change of entity_id. A later read compares the stored
name with what the registry holds now: equal means the name is still ours and
the type part can be read back without taking a rendered name apart; different
means the entity was renamed elsewhere, which is reported as drift rather than
quietly overwritten.

The file is built up as writes happen. An entity nobody has renamed through
this add-on has no entry, and that absence is itself the answer: the name
belongs to its integration or to whoever typed it.
"""

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from json_store import atomically, guarded, new_lock

logger = logging.getLogger(__name__)

# 2 tells an empty type part apart from one that was never recorded. Version 1
# wrote "" for both, so its entries cannot say which they mean and are migrated
# to "nothing recorded" on the first read.
SCHEMA_VERSION = 2

# Who the current registry name belongs to.
ENTITY_MANAGER = "entity_manager"  # we wrote it and it is unchanged
HA_UI = "ha_ui"  # we wrote it and it has been changed since
INTEGRATION = "integration"  # no name override at all; the integration supplies it
UNKNOWN = "unknown"  # a name override from before this file knew about it


class NamingState:
    """The names this add-on applied, one entry per registry id."""

    def __init__(self, storage_path: str = "naming_state.json") -> None:
        # One lock per store: a read-change-write stays one step.
        self._lock = new_lock()
        self.storage_path = Path(storage_path)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.storage_path.exists():
            return {"version": SCHEMA_VERSION, "entities": {}}
        try:
            with self.storage_path.open("r", encoding="utf-8") as file:
                data = json.load(file)
            if not isinstance(data, dict) or not isinstance(data.get("entities"), dict):
                raise ValueError("naming state must be an object with an entities map")
            self._forget_empty_type_parts_of_version_one(data)
            data["version"] = SCHEMA_VERSION
            return data
        except (OSError, ValueError, json.JSONDecodeError) as error:
            # A state file we cannot read is not worth failing a rename over:
            # starting empty costs provenance for the entities already named
            # and nothing else, and the next write begins filling it again.
            logger.error("Could not read %s, starting without it: %s", self.storage_path, error)
            return {"version": SCHEMA_VERSION, "entities": {}}

    @staticmethod
    def _forget_empty_type_parts_of_version_one(data: Dict[str, Any]) -> None:
        """Turn version 1's empty type parts into "nothing recorded".

        Version 1 stored ``""`` both for a name built out of area and device
        alone and for a caller that had nothing to say about the type part.
        Read as an answer, the second kind would strip the type part off every
        name it covers; read as a blank, the first kind would have it derived
        again. Neither can be told from the other, so the older entries say
        nothing and the name is taken apart once more - the type part it holds
        is then recorded properly, and the question does not come back.
        """
        # Anything from version 2 on has been through this. Asked as "is it the
        # current version", the next version to be written would have the
        # migration run over a version-2 file again and turn every "this name
        # has no type part" back into "nothing recorded" - which is the answer
        # this migration exists to stop being lost.
        if data.get("version", 0) >= 2:
            return
        for entry in data.get("entities", {}).values():
            if isinstance(entry, dict) and entry.get("base_entity") == "":
                entry["base_entity"] = None

    def _save(self) -> None:
        atomically(self.storage_path, self.data)

    @guarded
    def record(
        self,
        registry_id: str,
        *,
        applied_name: str,
        applied_entity_id: str,
        base_entity: Optional[str] = None,
        template_hash: str = "",
        won_by: str = "",
        rule_id: Optional[str] = None,
        applied_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Note that this add-on gave ``registry_id`` the name it now has."""
        if not registry_id:
            raise ValueError("a state entry needs the registry id of the entity")

        entry = {
            "applied_name": applied_name or "",
            "applied_entity_id": applied_entity_id or "",
            # None where the caller had nothing to say about the type part;
            # "" is a statement of its own - the name has no type part.
            "base_entity": base_entity if isinstance(base_entity, str) else None,
            "template_hash": template_hash or "",
            "won_by": won_by or "",
            "rule_id": rule_id,
            "applied_at": applied_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.data.setdefault("entities", {})[registry_id] = entry
        self._save()
        return dict(entry)

    @guarded
    def resupply(self, registry_id: str, base_entity: str) -> bool:
        """Correct which word an entry records the name as having been built from.

        Entries written before the type part was kept as it went in hold the
        word that came out of the rules instead. Nothing matches that word
        again, so a rule the user edits would never reach the entity. Where a
        read works out what actually went in, it is written back here, once.
        """
        entry = self.data.get("entities", {}).get(registry_id or "")
        if not isinstance(entry, dict) or entry.get("base_entity") == base_entity:
            return False
        # As it was given: "" is the statement that the name has no type part,
        # and record() keeps the same distinction.
        entry["base_entity"] = base_entity if isinstance(base_entity, str) else None
        self._save()
        return True

    def get(self, registry_id: str) -> Optional[Dict[str, Any]]:
        """The entry for ``registry_id``, or None if we never wrote its name."""
        entry = self.data.get("entities", {}).get(registry_id or "")
        return dict(entry) if isinstance(entry, dict) else None

    @guarded
    def forget(self, registry_id: str) -> bool:
        """Drop the entry, so the entity counts as never named here again."""
        removed = self.data.get("entities", {}).pop(registry_id or "", None) is not None
        if removed:
            self._save()
        return removed

    def count(self) -> int:
        """How many entities carry a name from this add-on."""
        return len(self.data.get("entities", {}))

    def ownership(self, entry: Dict[str, Any], template_hash: str = "") -> Dict[str, Any]:
        """Who the current name of a registry entry belongs to.

        ``entry`` is a registry entry as Home Assistant sends it. ``drift``
        means the name was changed outside this add-on; a template that has
        changed since is reported separately, because a different proposal is
        then expected and says nothing about who owns the name.
        """
        registry_id = (entry or {}).get("id") or ""
        current = (entry or {}).get("name") or ""
        stored = self.get(registry_id)

        if stored is None:
            return {
                "name_owner": UNKNOWN if current else INTEGRATION,
                "drift": False,
                "template_changed": False,
                "base_entity": "",
                "applied_name": "",
                "won_by": "",
                "rule_id": None,
            }

        drifted = current != stored["applied_name"]
        stale = bool(template_hash) and bool(stored["template_hash"]) and template_hash != stored["template_hash"]
        return {
            "name_owner": HA_UI if drifted else ENTITY_MANAGER,
            "drift": drifted,
            "template_changed": stale,
            "base_entity": stored.get("base_entity") or "",
            "applied_name": stored["applied_name"],
            "won_by": stored["won_by"],
            "rule_id": stored["rule_id"],
        }
