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

SCHEMA_VERSION = 1

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
            data["version"] = SCHEMA_VERSION
            return data
        except (OSError, ValueError, json.JSONDecodeError) as error:
            # A state file we cannot read is not worth failing a rename over:
            # starting empty costs provenance for the entities already named
            # and nothing else, and the next write begins filling it again.
            logger.error("Could not read %s, starting without it: %s", self.storage_path, error)
            return {"version": SCHEMA_VERSION, "entities": {}}

    def _save(self) -> None:
        atomically(self.storage_path, self.data)

    @guarded
    def record(
        self,
        registry_id: str,
        *,
        applied_name: str,
        applied_entity_id: str,
        base_entity: str = "",
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
            "base_entity": base_entity or "",
            "template_hash": template_hash or "",
            "won_by": won_by or "",
            "rule_id": rule_id,
            "applied_at": applied_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.data.setdefault("entities", {})[registry_id] = entry
        self._save()
        return dict(entry)

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
            "base_entity": stored["base_entity"],
            "applied_name": stored["applied_name"],
            "won_by": stored["won_by"],
            "rule_id": stored["rule_id"],
        }
