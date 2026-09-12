"""Replacing a device: the routes and the work behind them.

Swapping a device keeps its name, area and entity ids while pointing them at new
hardware. The long parts run as background jobs on the shared worker, so a swap
never blocks a request and never overlaps another registry change.
"""

import asyncio
from datetime import datetime, timezone
import logging
import os
import uuid

from flask import Blueprint, jsonify, request

from app_state import ensure_mqtt_bridge, init_client, renamer_state, ws_url
from bridge_adapters import build_bridge
from dependency_updater import DependencyUpdater
from device_registry import DeviceRegistry
import device_swap
from device_swap import SwapExecutor, propose_mapping
from entity_registry import EntityRegistry
from ha_websocket import HomeAssistantWebSocket
from jobs import new_job
from lovelace_updater import LovelaceUpdater
from reference_checker import ReferenceChecker
from sanitize import sanitize_entity_id

logger = logging.getLogger(__name__)

swap = Blueprint("swap", __name__)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _device_snapshot(restructurer, device_id: str) -> dict:
    """Erzeugt einen kompakten, persistierbaren Snapshot eines Geräts."""
    from integration_bridge import extract_integrations

    d = restructurer.devices.get(device_id, {}) or {}
    return {
        "device_id": device_id,
        "name": d.get("name_by_user") or d.get("name") or "",
        "integrations": extract_integrations(d),
        "config_entries": d.get("config_entries", []),
        "identifiers": d.get("identifiers", []),
    }


def _device_entities(restructurer, device_id: str) -> list:
    """Alle Entity-Registry-Einträge eines Geräts."""
    return [e for e in restructurer.entities.values() if e.get("device_id") == device_id]


@swap.route("/api/bridge/status", methods=["GET"])
def bridge_status():
    """Status der Integrations-Bridge (welche nativen Operationen möglich sind)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        bridge = loop.run_until_complete(ensure_mqtt_bridge())
    except Exception as e:  # noqa: BLE001 - Status darf nie crashen
        logger.warning("bridge_status MQTT check failed: %s", e)
        bridge = None
    finally:
        loop.close()

    z2m_ok = bridge is not None and getattr(bridge, "connected", False)
    return jsonify(
        {
            "mqtt_available": z2m_ok,
            "z2m_supported": z2m_ok,
            "matter_remove_supported": True,
            "z2m_enabled": os.getenv("ENABLE_Z2M_BRIDGE", "true").lower() == "true",
        }
    )


@swap.route("/api/swap/devices", methods=["GET"])
def swap_devices():
    """Liste aller Geräte (für die Auswahl im Swap-Wizard)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_swap_devices_async())
    finally:
        loop.close()


async def _swap_devices_async():
    from integration_bridge import extract_integrations

    await init_client()
    ws = HomeAssistantWebSocket(ws_url(), os.getenv("HA_TOKEN"))
    await ws.connect()
    try:
        await renamer_state["restructurer"].load_structure(ws)
    finally:
        await ws.disconnect()

    restructurer = renamer_state["restructurer"]
    areas = {aid: a.get("name", "") for aid, a in restructurer.areas.items()}
    devices = []
    for device_id, d in restructurer.devices.items():
        entity_count = len(_device_entities(restructurer, device_id))
        devices.append(
            {
                "device_id": device_id,
                "name": d.get("name_by_user") or d.get("name") or "",
                "area": areas.get(d.get("area_id"), ""),
                "area_id": d.get("area_id"),
                "integrations": extract_integrations(d),
                "entity_count": entity_count,
            }
        )
    devices.sort(key=lambda x: (x["area"] or "~", x["name"]))
    return jsonify({"devices": devices})


@swap.route("/api/swap/jobs", methods=["GET"])
def swap_jobs():
    """Nicht abgeschlossene Swap-Jobs (für Resume)."""
    jobs = renamer_state["swap_store"].list_unfinished()
    return jsonify({"jobs": jobs})


@swap.route("/api/swap/<job_id>", methods=["GET"])
def swap_job_get(job_id):
    """Aktueller Stand eines Swap-Jobs."""
    job = renamer_state["swap_store"].load(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@swap.route("/api/swap/propose", methods=["POST"])
def swap_propose():
    """Legt einen Swap-Job an und schlägt ein Entity-Mapping vor."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_swap_propose_async())
    finally:
        loop.close()


async def _swap_propose_async():
    data = request.json or {}
    old_id = (data.get("old_device_id") or "").strip()
    new_id = (data.get("new_device_id") or "").strip()
    if not old_id or not new_id:
        return jsonify({"error": "old_device_id and new_device_id required"}), 400
    if old_id == new_id:
        return jsonify({"error": "old and new device must differ"}), 400

    client = await init_client()
    states = await client.get_states()
    dashboard_refs = set()
    ws = HomeAssistantWebSocket(ws_url(), os.getenv("HA_TOKEN"))
    await ws.connect()
    try:
        await renamer_state["restructurer"].load_structure(ws)
        dashboard_refs = await LovelaceUpdater(ws).get_referenced_entity_ids()
    finally:
        await ws.disconnect()

    restructurer = renamer_state["restructurer"]
    if old_id not in restructurer.devices or new_id not in restructurer.devices:
        return jsonify({"error": "Unknown device id"}), 404

    states_by_id = {s["entity_id"]: s for s in states}
    old_ents = _device_entities(restructurer, old_id)
    new_ents = _device_entities(restructurer, new_id)

    # Nur referenzierte (in use) alte Entities mappen - ungenutzte werden ohnehin
    # über die Device-Rename-Logik mitbenannt und brauchen kein Mapping.
    # in use = Automations/Scenes/Scripts (REST) + Dashboards (WS).
    ref_checker = ReferenceChecker(os.getenv("HA_URL"), os.getenv("HA_TOKEN"))
    referenced = await ref_checker.get_all_referenced_entity_ids()
    referenced |= dashboard_refs

    # Präfixe über ALLE Entities bestimmen, gemappt werden nur die in-use.
    proposal = propose_mapping(old_ents, new_ents, states_by_id, in_use_ids=referenced)
    proposal["old_total"] = len(old_ents)
    proposal["old_in_use"] = len([e for e in old_ents if e.get("entity_id") in referenced])

    old_snap = _device_snapshot(restructurer, old_id)
    new_snap = _device_snapshot(restructurer, new_id)

    now = _iso_now()
    job = {
        "version": device_swap.SCHEMA_VERSION,
        "job_id": uuid.uuid4().hex,
        "created": now,
        "updated": now,
        "state": device_swap.STATE_PROPOSED,
        "old_device": old_snap,
        "new_device": new_snap,
        "target_device_name": old_snap["name"],
        "old_device_disposition": device_swap.DISPOSITION_KEEP,
        # ALLE alten Entities (müssen freigemacht werden) und ALLE neuen (werden umbenannt)
        "old_device_entities": sorted(e["entity_id"] for e in old_ents),
        "new_device_entities": sorted(e["entity_id"] for e in new_ents),
        "proposal": proposal,
        "entity_mapping": [],
        "steps": {},
        "log": [],
    }
    renamer_state["swap_store"].save(job)
    return jsonify(job)


@swap.route("/api/swap/<job_id>/confirm", methods=["POST"])
def swap_confirm(job_id):
    """Bestätigt Mapping + Disposition und friert den Job ein (CONFIRMED)."""
    data = request.json or {}
    store = renamer_state["swap_store"]
    job = store.load(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["state"] not in (device_swap.STATE_PROPOSED, device_swap.STATE_CONFIRMED):
        return jsonify({"error": f"Job cannot be confirmed in state {job['state']}"}), 409

    mapping = data.get("entity_mapping") or []
    disposition = data.get("old_device_disposition", device_swap.DISPOSITION_KEEP)
    valid_dispositions = {
        device_swap.DISPOSITION_KEEP,
        device_swap.DISPOSITION_DISABLE,
        device_swap.DISPOSITION_DELETE,
    }
    if disposition not in valid_dispositions:
        return jsonify({"error": "Invalid old_device_disposition"}), 400

    entity_mapping = []
    for pair in mapping:
        old_e = sanitize_entity_id(pair.get("old_entity_id"))
        new_e = sanitize_entity_id(pair.get("new_entity_id"))
        if not old_e or not new_e:
            continue
        entity_mapping.append({"old_entity_id": old_e, "new_entity_id_current": new_e, "status": "pending"})

    # Leeres Mapping ist zulässig (keine verwendeten Entities) - dann werden nur
    # Geräte umbenannt/behandelt, ohne Referenzen umzubiegen.
    job["entity_mapping"] = entity_mapping
    job["old_device_disposition"] = disposition
    job["state"] = device_swap.STATE_CONFIRMED
    job["updated"] = _iso_now()
    store.save(job)
    return jsonify(job)


@swap.route("/api/swap/<job_id>/execute", methods=["POST"])
def swap_execute(job_id):
    """Enqueue swap execution/continuation as a background job (idempotent).

    The swap keeps its own persisted state machine and resume flow in swap_store;
    the worker just runs it serially, off the request path. The frontend polls
    api/swap/<job_id> for progress. Returns the swap job so the UI can start
    polling immediately.
    """
    store = renamer_state["swap_store"]
    job = store.load(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["state"] in (device_swap.STATE_PROPOSED, device_swap.STATE_ABORTED, device_swap.STATE_COMPLETED):
        return jsonify({"error": f"Job not runnable in state {job['state']}"}), 409

    generic = new_job("swap", {"swap_job_id": job_id}, job_id=uuid.uuid4().hex)
    renamer_state["job_store"].save(generic)
    renamer_state["worker"].enqueue(generic)
    return jsonify(job), 202


async def swap_execute_handler(job, ctx):
    """Run/continue a device swap inside the worker.

    Loads the swap job from swap_store and drives its SwapExecutor state machine.
    The executor persists progress per step/entity in swap_store (which the UI
    polls); this generic wrapper job only records that the run happened.
    """
    swap_job_id = job["payload"]["swap_job_id"]
    store = renamer_state["swap_store"]
    swap_job = store.load(swap_job_id)
    if not swap_job:
        raise RuntimeError(f"Swap job {swap_job_id} not found")

    client = await init_client()
    states = await client.get_states()
    token = os.getenv("HA_TOKEN")
    ws = HomeAssistantWebSocket(ws_url(), token)
    await ws.connect()
    try:
        await renamer_state["restructurer"].load_structure(ws)
        device_registry = DeviceRegistry(ws)
        entity_registry = EntityRegistry(ws)
        dependency_updater = DependencyUpdater(os.getenv("HA_URL"), token)
        bridge = build_bridge(device_registry, mqtt_bridge=await ensure_mqtt_bridge())
        executor = SwapExecutor(
            store=store,
            device_registry=device_registry,
            entity_registry=entity_registry,
            dependency_updater=dependency_updater,
            bridge=bridge,
            restructurer=renamer_state["restructurer"],
            states_by_id={s["entity_id"]: s for s in states},
            timestamp=_iso_now(),
            lovelace_updater=LovelaceUpdater(ws),
        )
        swap_job = await executor.run(swap_job)
    finally:
        await ws.disconnect()

    return {"swap_job_id": swap_job_id, "final_state": swap_job.get("state")}


renamer_state["worker"].register("swap", swap_execute_handler)


@swap.route("/api/swap/<job_id>/abort", methods=["POST"])
def swap_abort(job_id):
    """Bricht einen noch nicht ausgeführten Job ab."""
    store = renamer_state["swap_store"]
    job = store.load(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job["state"] not in (device_swap.STATE_PROPOSED, device_swap.STATE_CONFIRMED):
        return jsonify({"error": "Job already started; cannot abort, use resume instead"}), 409
    # Vor der Ausführung wurde nichts am System geändert -> Job ganz entfernen (keine Leiche).
    store.delete(job_id)
    return jsonify({"success": True, "deleted": job_id})
