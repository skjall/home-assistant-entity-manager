"""Exceptions become rules that name one entity.

An exception and a rule always said the same thing - "call this X" - and
differed only in how far they reached. Now that a rule can carry a filter
naming a single entity by its registry id, the exception is that rule, and
keeping a second store for it only split one list in two.

So the exceptions are adopted into the rules once, and what is left in the
override store is the one thing that is not a name at all: an entity the user
asked to be left alone.

Both steps need the registry, because an exception knows only a registry id
and a rule wants to say which type it is about. They therefore run after the
registry is read, not when the files are loaded.
"""

from datetime import datetime, timezone
import logging
from pathlib import Path
import shutil
from typing import Any, Dict, Optional

from naming_canon import canon
from naming_rules import NamingRuleError

logger = logging.getLogger(__name__)


def _back_up(*stores) -> Optional[str]:
    """Copy the files this is about to rewrite, next to where they live.

    The same place and shape the other migrations use, so a user who wants the
    old list back finds it where the old lists already are.
    """
    paths = [Path(getattr(store, "storage_path", "")) for store in stores]
    paths = [path for path in paths if path and path.exists()]
    if not paths:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = paths[0].parent / "migrations" / stamp
    folder.mkdir(parents=True, exist_ok=True)
    for path in paths:
        shutil.copy2(path, folder / path.name)
    return str(folder)


def _entities_by_registry_id(restructurer) -> Dict[str, Dict[str, Any]]:
    return {
        entity["id"]: {"entity_id": entity_id, "entry": entity}
        for entity_id, entity in (getattr(restructurer, "entities", None) or {}).items()
        if entity.get("id")
    }


def _supplied_key(entry: Dict[str, Any], fallback: str) -> str:
    """What this entity is about, for a reader of the rule list.

    A rule written for one entity is found by that entity, never by this value,
    so it only has to say something recognisable. The name the integration
    supplies does; where there is none, the user's own wording has to.
    """
    return canon(entry.get("original_name") or "") or canon(fallback) or "entity"


def forget_missing(overrides, rules, restructurer) -> Dict[str, int]:
    """Drop every exception and entity rule whose entity is gone.

    Runs on every registry read rather than on request: an entity that has been
    removed is not coming back under the same registry id, and an entry nobody
    can see is an entry nobody can delete either.

    A registry that was not read is no evidence that anything is gone, so an
    empty one stops this before it removes what it cannot check.
    """
    known = _entities_by_registry_id(restructurer)
    if not known:
        return {"exceptions": 0, "rules": 0}

    gone = [registry_id for registry_id in overrides.get_all_entity_overrides() if registry_id not in known]
    for registry_id in gone:
        overrides.remove_entity_override(registry_id)

    stale = []
    for rule in list(rules.rules):
        named = rules._entities_of(rule)
        if named and all(registry_id not in known for registry_id in named):
            stale.append(rule["id"])
    if stale:
        rules.delete_many(stale)

    if gone or stale:
        logger.info("Forgot %d exceptions and %d rules whose entity is gone", len(gone), len(stale))
    return {"exceptions": len(gone), "rules": len(stale)}


def adopt_exceptions(overrides, rules, restructurer) -> Optional[Dict[str, Any]]:
    """Turn every exception that names an entity into a rule for that entity.

    Once, and recorded in the rules file so a later start leaves alone what the
    user has since changed. An entity the user asked to keep its own name stays
    in the override store: that is not a name, it is the absence of one.
    """
    if rules.data.get("exceptions_adopted"):
        return None

    known = _entities_by_registry_id(restructurer)
    if not known:
        return None

    backup = _back_up(overrides, rules)
    language = rules.language
    adopted, kept, failed = [], [], []
    for registry_id, entry in list(overrides.get_all_entity_overrides().items()):
        name = (entry or {}).get("name") or ""
        if (entry or {}).get("keep_original") or not name:
            kept.append(registry_id)
            continue
        found = known.get(registry_id)
        if not found:
            # forget_missing has the say over these; do not invent a rule for
            # an entity nobody can point at.
            continue
        try:
            rule = rules.upsert_for_entity(
                registry_id,
                _supplied_key(found["entry"], name),
                language,
                name,
                learned_from=found["entity_id"],
            )
        except NamingRuleError as error:
            failed.append({"registry_id": registry_id, "error": str(error)})
            continue
        overrides.remove_entity_override(registry_id)
        adopted.append({"registry_id": registry_id, "entity_id": found["entity_id"], "rule_id": rule["id"]})

    report = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "backup": backup,
        "adopted": len(adopted),
        "kept": len(kept),
        "failed": failed,
    }
    rules.data["exceptions_adopted"] = report
    rules.save()
    logger.info("Adopted %d exceptions as rules, left %d alone, %d failed", len(adopted), len(kept), len(failed))
    return report
