"""Append-only audit log of entity_id renames (old -> new).

Records are written one JSON object per line (JSONL) so an external consumer
(e.g. a Home Assistant AI skill) can look up where a vanished entity_id was
renamed to. Records are intentionally compact:

    {"timestamp": ..., "old_entity_id": ..., "new_entity_id": ..., "friendly_name": ...}

Renames can chain (``a -> b`` and later ``b -> c``). The search resolves a
queried entity_id forward through the chain to its current id.
"""

from datetime import datetime, timezone
import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class RenameLog:
    """Persistent, append-only log of entity_id renames."""

    def __init__(self, path: str) -> None:
        """Initialise the log backed by the JSONL file at ``path``.

        The parent directory is created if it does not exist.
        """
        self.path = path
        self._lock = threading.Lock()
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def record(
        self,
        old_entity_id: str,
        new_entity_id: str,
        friendly_name: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> None:
        """Append a single rename record.

        No-ops when the rename is missing ids or is not an actual id change
        (``old_entity_id == new_entity_id``). Failures are logged but never
        raised, so audit logging cannot break a rename operation.
        """
        if not old_entity_id or not new_entity_id or old_entity_id == new_entity_id:
            return

        entry: Dict[str, Any] = {
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
            "old_entity_id": old_entity_id,
            "new_entity_id": new_entity_id,
            "friendly_name": friendly_name,
        }
        try:
            line = json.dumps(entry, ensure_ascii=False)
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError as error:
            logger.warning("Failed to write rename audit log entry: %s", error)

    def _read_all(self) -> List[Dict[str, Any]]:
        """Return all records in write order. Missing file yields an empty list."""
        records: List[Dict[str, Any]] = []
        try:
            with self._lock:
                with open(self.path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            logger.warning("Skipping malformed rename audit log line")
        except FileNotFoundError:
            return []
        except OSError as error:
            logger.warning("Failed to read rename audit log: %s", error)
        return records

    def search(self, entity_id: str) -> Dict[str, Any]:
        """Resolve ``entity_id`` to its current id by following the rename chain.

        Treats ``entity_id`` as an old (possibly vanished) id and walks forward
        through successive renames. Returns a single answer rather than a list:

            {
                "query": "light.old",
                "found": true,
                "renamed": true,
                "current_entity_id": "light.new",
                "history": [ {record}, ... ]   # oldest -> newest hop
            }

        ``found`` is ``False`` when the id never appears as an ``old_entity_id``
        in the log (it was never renamed, or is unknown).
        """
        records = self._read_all()
        # Index the latest rename per old_entity_id (last write wins).
        latest_by_old: Dict[str, Dict[str, Any]] = {}
        for record in records:
            old = record.get("old_entity_id")
            if old:
                latest_by_old[old] = record

        history: List[Dict[str, Any]] = []
        current = entity_id
        seen = {current}
        while current in latest_by_old:
            hop = latest_by_old[current]
            history.append(hop)
            current = hop.get("new_entity_id")
            if not current or current in seen:
                # Guard against cycles (e.g. a -> b -> a).
                break
            seen.add(current)

        if not history:
            return {"query": entity_id, "found": False, "renamed": False}

        return {
            "query": entity_id,
            "found": True,
            "renamed": True,
            "current_entity_id": current,
            "history": history,
        }
