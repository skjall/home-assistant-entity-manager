#!/usr/bin/env python3
"""
Device Swap - Geräte-Austausch als persistenter, wiederaufnehmbarer Workflow.

Use case: Ein Hardware-Gerät wird physisch durch ein neues ersetzt (z.B. Eve
Fenstersensor -> IKEA-Sensor). In HA entsteht ein neues Device mit neuen Entities
und anderen entity_id-slugs. Damit alle Automations/Szenen/Skripte/Dashboards
weiterlaufen, müssen die Referenzen vom alten auf das neue Gerät umgebogen werden.

Der Vorgang ist mehrstufig und teils destruktiv. Damit ein Browser-/Container-
Crash nichts kaputt macht, wird der Fortschritt als JSON-Job in /data/device_swaps
persistiert (atomar geschrieben) und ist jederzeit idempotent wiederaufnehmbar.

Dieses Modul enthält die Persistenz (SwapJobStore), den Mapping-Vorschlag
(propose_mapping) und die State-Machine (SwapExecutor). Die HA-Clients werden
injiziert; dieses Modul kennt keine Flask-/Request-Details.
"""

import difflib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# State-Machine (lineare Reihenfolge). Kritische Invariante: Referenzen werden
# umgebogen (UPDATING_DEPENDENCIES), BEVOR das alte Gerät nativ entfernt wird.
STATE_PROPOSED = "PROPOSED"
STATE_CONFIRMED = "CONFIRMED"
STATE_FREEING_OLD_NAME = "FREEING_OLD_NAME"
STATE_RENAMING_NEW_DEVICE = "RENAMING_NEW_DEVICE"
STATE_RENAMING_ENTITIES = "RENAMING_ENTITIES"
STATE_UPDATING_DEPENDENCIES = "UPDATING_DEPENDENCIES"
STATE_DISPOSING_OLD_DEVICE = "DISPOSING_OLD_DEVICE"
STATE_NATIVE_REMOVE = "NATIVE_REMOVE"
STATE_COMPLETED = "COMPLETED"
STATE_FAILED = "FAILED"
STATE_ABORTED = "ABORTED"

# Reihenfolge der ausführenden Schritte (PROPOSED/CONFIRMED sind vor der Ausführung).
EXECUTION_SEQUENCE = [
    STATE_FREEING_OLD_NAME,
    STATE_RENAMING_NEW_DEVICE,
    STATE_RENAMING_ENTITIES,
    STATE_UPDATING_DEPENDENCIES,
    STATE_DISPOSING_OLD_DEVICE,
    STATE_NATIVE_REMOVE,
]

DISPOSITION_KEEP = "keep"
DISPOSITION_DISABLE = "disable"
DISPOSITION_DELETE = "delete"


# --------------------------------------------------------------------------- #
# Persistenz
# --------------------------------------------------------------------------- #


class SwapJobStore:
    """Persistiert Swap-Jobs als einzelne JSON-Dateien unter /data/device_swaps."""

    def __init__(self, storage_dir: str = "/data/device_swaps"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, job_id: str) -> Path:
        # job_id ist ein uuid4-hex; defensiv nur den Basename verwenden.
        safe = os.path.basename(job_id)
        return self.storage_dir / f"{safe}.json"

    def save(self, job: Dict[str, Any]) -> None:
        """Schreibt einen Job atomar (temp-Datei + os.replace)."""
        job["version"] = SCHEMA_VERSION
        path = self._path(job["job_id"])
        tmp = path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(job, f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as e:
            logger.error(f"Failed to save swap job {job.get('job_id')}: {e}")
            raise

    def load(self, job_id: str) -> Optional[Dict[str, Any]]:
        path = self._path(job_id)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Failed to load swap job {job_id}: {e}")
            return None

    def list_jobs(self) -> List[Dict[str, Any]]:
        jobs = []
        for path in sorted(self.storage_dir.glob("*.json")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    jobs.append(json.load(f))
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Skipping unreadable swap job {path.name}: {e}")
        return jobs

    def list_unfinished(self) -> List[Dict[str, Any]]:
        """Alle Jobs, die noch fortgesetzt werden können (für Resume-UI)."""
        terminal = {STATE_COMPLETED, STATE_ABORTED}
        return [j for j in self.list_jobs() if j.get("state") not in terminal]

    def delete(self, job_id: str) -> None:
        path = self._path(job_id)
        if path.exists():
            path.unlink()


# --------------------------------------------------------------------------- #
# Mapping-Vorschlag
# --------------------------------------------------------------------------- #


def _object_id(entity_id: str) -> str:
    """Teil hinter dem Punkt (z.B. light.kueche -> kueche)."""
    return entity_id.split(".", 1)[1] if "." in entity_id else entity_id


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def _device_class_for(entity_id: str, states_by_id: Dict[str, Dict]) -> Optional[str]:
    state = states_by_id.get(entity_id) or {}
    return (state.get("attributes") or {}).get("device_class")


def _name_similarity(a: str, b: str) -> float:
    """Ähnlichkeit zweier object_ids (0..1), case-insensitiv."""
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def propose_mapping(
    old_entities: List[Dict[str, Any]],
    new_entities: List[Dict[str, Any]],
    states_by_id: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Schlägt ein Entity-Mapping zwischen altem und neuem Gerät vor.

    Strategie: zuerst exakte (domain, device_class)-Paare, bei mehreren Kandidaten
    nach Namensähnlichkeit; danach reiner Namensabgleich innerhalb der Domain.
    Jede neue Entity wird höchstens einmal zugeordnet.

    Args:
        old_entities: Entity-Registry-Einträge des alten Geräts (mit "entity_id").
        new_entities: Entity-Registry-Einträge des neuen Geräts.
        states_by_id: Mapping entity_id -> State (für device_class).

    Returns:
        {"pairs": [{old_entity_id,new_entity_id,device_class,confidence}],
         "unmapped_old": [entity_id...], "unmapped_new": [entity_id...]}
    """
    old_ids = [e["entity_id"] for e in old_entities]
    new_ids = [e["entity_id"] for e in new_entities]
    remaining_new = list(new_ids)

    pairs: List[Dict[str, Any]] = []

    def take_best(old_id: str, candidates: List[str], base_conf: float) -> Optional[Dict[str, Any]]:
        if not candidates:
            return None
        old_obj = _object_id(old_id)
        best = max(candidates, key=lambda nid: _name_similarity(old_obj, _object_id(nid)))
        sim = _name_similarity(old_obj, _object_id(best))
        confidence = round(min(1.0, base_conf + 0.3 * sim), 2)
        return {
            "old_entity_id": old_id,
            "new_entity_id": best,
            "device_class": _device_class_for(old_id, states_by_id),
            "confidence": confidence,
        }

    # 1) Match über gleiche Domain + device_class
    for old_id in old_ids:
        dom = _domain(old_id)
        dc = _device_class_for(old_id, states_by_id)
        candidates = [
            nid for nid in remaining_new if _domain(nid) == dom and _device_class_for(nid, states_by_id) == dc
        ]
        # Nur als "device_class-Match" werten, wenn device_class gesetzt ist;
        # sonst ist es nur ein Domain-Match (schwächer) -> in Schritt 2.
        if dc is not None and candidates:
            pair = take_best(old_id, candidates, base_conf=0.6)
            if pair:
                pairs.append(pair)
                remaining_new.remove(pair["new_entity_id"])

    matched_old = {p["old_entity_id"] for p in pairs}

    # 2) Restliche: Match nur über Domain + Namensähnlichkeit
    for old_id in old_ids:
        if old_id in matched_old:
            continue
        dom = _domain(old_id)
        candidates = [nid for nid in remaining_new if _domain(nid) == dom]
        pair = take_best(old_id, candidates, base_conf=0.3)
        if pair and pair["confidence"] >= 0.4:
            pairs.append(pair)
            remaining_new.remove(pair["new_entity_id"])
            matched_old.add(old_id)

    unmapped_old = [oid for oid in old_ids if oid not in matched_old]
    unmapped_new = list(remaining_new)

    return {"pairs": pairs, "unmapped_old": unmapped_old, "unmapped_new": unmapped_new}


# --------------------------------------------------------------------------- #
# State-Machine-Ausführung
# --------------------------------------------------------------------------- #


class SwapExecutor:
    """Führt einen bestätigten Swap-Job idempotent aus und persistiert jeden Schritt.

    Die HA-Clients werden injiziert. Jeder Schritt schreibt seinen Status VOR der
    Ausführung auf "started" und nach Erfolg auf "done"; bei einem Crash dazwischen
    wird der Schritt beim Resume erneut ausgeführt (alle Schritte sind idempotent).
    """

    def __init__(
        self,
        store: "SwapJobStore",
        device_registry: Any,
        entity_registry: Any,
        dependency_updater: Any,
        bridge: Any,
        restructurer: Any,
        states_by_id: Optional[Dict[str, Dict[str, Any]]] = None,
        timestamp: str = "",
    ):
        self.store = store
        self.device_registry = device_registry
        self.entity_registry = entity_registry
        self.dependency_updater = dependency_updater
        self.bridge = bridge
        self.restructurer = restructurer
        self.states_by_id = states_by_id or {}
        self.timestamp = timestamp

    # --- Persistenz-Helfer ---

    def _log(self, job: Dict[str, Any], step: str, message: str) -> None:
        job.setdefault("log", []).append({"ts": self.timestamp, "step": step, "message": message})
        logger.info(f"[swap {job['job_id']}] {step}: {message}")

    def _step(self, job: Dict[str, Any], step: str) -> Dict[str, Any]:
        return job.setdefault("steps", {}).setdefault(step, {"status": "pending"})

    def _persist(self, job: Dict[str, Any]) -> None:
        job["updated"] = self.timestamp
        self.store.save(job)

    # --- Öffentliche Ausführung ---

    async def run(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """Führt alle noch offenen Schritte ab dem aktuellen Stand aus.

        Bricht beim ersten Fehler ab (Job bleibt im letzten konsistenten Zustand,
        FAILED) und ist danach erneut aufrufbar (Resume).
        """
        if job.get("state") in (STATE_COMPLETED, STATE_ABORTED):
            return job

        handlers = {
            STATE_FREEING_OLD_NAME: self._free_old_name,
            STATE_RENAMING_NEW_DEVICE: self._rename_new_device,
            STATE_RENAMING_ENTITIES: self._rename_entities,
            STATE_UPDATING_DEPENDENCIES: self._update_dependencies,
            STATE_DISPOSING_OLD_DEVICE: self._dispose_old_device,
            STATE_NATIVE_REMOVE: self._native_remove,
        }

        for state in EXECUTION_SEQUENCE:
            step = self._step(job, state)
            if step.get("status") == "done":
                continue

            job["state"] = state
            step["status"] = "started"
            self._persist(job)

            try:
                await handlers[state](job)
                step["status"] = "done"
                self._persist(job)
            except Exception as e:  # noqa: BLE001 - Fehler festhalten und Job pausieren
                step["status"] = "failed"
                step["error"] = str(e)
                job["state"] = STATE_FAILED
                job["failed_step"] = state
                self._log(job, state, f"FAILED: {e}")
                self._persist(job)
                logger.error(f"Swap job {job['job_id']} failed at {state}: {e}")
                return job

        job["state"] = STATE_COMPLETED
        self._log(job, STATE_COMPLETED, "Swap completed")
        self._persist(job)
        return job

    # --- Einzelschritte (idempotent) ---

    async def _free_old_name(self, job: Dict[str, Any]) -> None:
        """Altes Gerät freimachen: Device + ALLE alten Entities umbenennen, OHNE Cascade.

        Macht die Ziel-IDs (z.B. kuche_fenster_*) frei, damit die neuen Entities sie
        übernehmen können. Die Dependencies zeigen weiter auf die *ursprünglichen* alten
        IDs und werden erst in UPDATING_DEPENDENCIES umgebogen.
        """
        old = job["old_device"]
        temp_name = job.get("old_device_temp_name") or f"{old['name']} (swap-out)"
        job["old_device_temp_name"] = temp_name
        await self.device_registry.rename_device(old["device_id"], temp_name)
        self._log(job, STATE_FREEING_OLD_NAME, f"Old device renamed to '{temp_name}'")

        freed = job.setdefault("old_freed", {})
        for old_id in job.get("old_device_entities", []):
            if old_id in freed:
                continue  # idempotent (Resume)
            domain, _, obj = old_id.partition(".")
            temp_id = f"{domain}.{obj}_swapout"
            await self.entity_registry.rename_entity(old_id, temp_id)
            freed[old_id] = temp_id
            self._log(job, STATE_FREEING_OLD_NAME, f"Freed old entity {old_id} -> {temp_id}")
            self._persist(job)

    async def _rename_new_device(self, job: Dict[str, Any]) -> None:
        """Neues Gerät bekommt den (jetzt freien) Zielnamen (= ursprünglicher alter Name)."""
        new = job["new_device"]
        target = job["target_device_name"]
        await self.device_registry.rename_device(new["device_id"], target)
        self._log(job, STATE_RENAMING_NEW_DEVICE, f"New device renamed to '{target}'")
        # Struktur neu laden, damit generate_new_entity_id den neuen Device-Namen kennt.
        if hasattr(self.restructurer, "load_structure"):
            await self.restructurer.load_structure(self.entity_registry.ws)

    async def _rename_entities(self, job: Dict[str, Any]) -> None:
        """ALLE Entities des neuen Geräts auf saubere Ziel-IDs bringen (Identität des alten Geräts).

        Nutzt generate_new_entity_id (das neue Device trägt jetzt den Namen des alten),
        sodass der Bereichs-/Device-Präfix übernommen wird und der Entity-Suffix bleibt.
        Pro-Entity idempotent über new_renamed.
        """
        renamed = job.setdefault("new_renamed", {})
        for current in job.get("new_device_entities", []):
            if current in renamed:
                continue  # idempotent (Resume)
            state = self.states_by_id.get(current, {})
            target = current
            friendly = None
            try:
                gen_id, gen_friendly = self.restructurer.generate_new_entity_id(current, state)
                if gen_id:
                    target = gen_id
                    friendly = gen_friendly
            except Exception as e:  # noqa: BLE001 - generate ist best effort
                self._log(job, STATE_RENAMING_ENTITIES, f"generate_new_entity_id failed for {current}: {e}")

            if target != current:
                await self.entity_registry.rename_entity(current, target, friendly)
                self._log(job, STATE_RENAMING_ENTITIES, f"Renamed entity {current} -> {target}")
            renamed[current] = target
            self._persist(job)

    async def _update_dependencies(self, job: Dict[str, Any]) -> None:
        """Referenzen umbiegen: ursprüngliche alte ID -> finale neue ID (pro Paar idempotent)."""
        states = list(self.states_by_id.values()) or None
        renamed = job.get("new_renamed", {})
        for pair in job["entity_mapping"]:
            if pair.get("status") == "deps_done":
                continue
            old_id = pair["old_entity_id"]
            current = pair["new_entity_id_current"]
            new_id = renamed.get(current, current)  # finale ID nach RENAMING_ENTITIES
            await self.dependency_updater.update_all_dependencies(old_id, new_id, states)
            # Dashboards werden in einer späteren Ausbaustufe hier ergänzt.
            pair["new_entity_id_target"] = new_id
            pair["status"] = "deps_done"
            self._log(job, STATE_UPDATING_DEPENDENCIES, f"Rewired references {old_id} -> {new_id}")
            self._persist(job)

    async def _dispose_old_device(self, job: Dict[str, Any]) -> None:
        """Altes Gerät je nach Wahl behalten/deaktivieren/löschen (HA-Registry-Ebene)."""
        disposition = job.get("old_device_disposition", DISPOSITION_KEEP)
        old = job["old_device"]
        if disposition == DISPOSITION_KEEP:
            final_name = f"{old['name']} (ersetzt)"
            await self.device_registry.rename_device(old["device_id"], final_name)
            self._log(job, STATE_DISPOSING_OLD_DEVICE, f"Kept old device as '{final_name}'")
        elif disposition == DISPOSITION_DISABLE:
            await self.device_registry.disable_device(old["device_id"])
            self._log(job, STATE_DISPOSING_OLD_DEVICE, "Disabled old device")
        elif disposition == DISPOSITION_DELETE:
            # Eigentliches Entfernen passiert in NATIVE_REMOVE (nach allen Updates).
            self._log(job, STATE_DISPOSING_OLD_DEVICE, "Marked old device for native removal")

    async def _native_remove(self, job: Dict[str, Any]) -> None:
        """Bei disposition=delete: Gerät nativ aus der Integration entfernen (Re-Population-Schutz)."""
        if job.get("old_device_disposition") != DISPOSITION_DELETE:
            return
        old = job["old_device"]
        result = await self.bridge.remove_native(old, force=True)
        if not result.success:
            # Teilerfolg: Referenzen sind umgebogen, aber Gerät blieb (z.B. Matter rejected).
            self._log(
                job,
                STATE_NATIVE_REMOVE,
                f"Native removal not completed ({result.error}); references already rewired",
            )
            raise Exception(f"Native removal failed: {result.error}")
        self._log(job, STATE_NATIVE_REMOVE, "Old device natively removed")
