"""Tests for the device swap engine: mapping, prefix swap, friendly names, executor flow."""

import device_swap
from device_swap import SwapExecutor, SwapJobStore, _common_prefix_tokens, _entity_name, propose_mapping

# --------------------------------------------------------------------------- #
# Pure helpers: prefix / suffix
# --------------------------------------------------------------------------- #


def test_common_prefix_tokens():
    obj_ids = ["kuche_fenster_zustand", "kuche_fenster_batterie", "kuche_fenster_firmware"]
    assert _common_prefix_tokens(obj_ids) == ["kuche", "fenster"]


def test_common_prefix_keeps_last_token():
    # last token always survives so the suffix is never empty
    assert _common_prefix_tokens(["kuche_fenster_zustand"]) == ["kuche", "fenster"]


def test_entity_name_strips_prefix():
    assert _entity_name("sensor.kuche_fenster_ikea_batterie", ["kuche", "fenster", "ikea"]) == "batterie"


# --------------------------------------------------------------------------- #
# propose_mapping: exact suffix match, in-use filter
# --------------------------------------------------------------------------- #


def _ents(ids):
    return [{"entity_id": i} for i in ids]


OLD = _ents(
    [
        "sensor.kuche_fenster_batterie",
        "sensor.kuche_fenster_batteriespannung",
        "button.kuche_fenster_identifizieren",
        "binary_sensor.kuche_fenster_zustand",
    ]
)
NEW = _ents(
    [
        "sensor.kuche_fenster_ikea_batterie",
        "sensor.kuche_fenster_ikea_batteriespannung",
        "button.kuche_fenster_ikea_taste",
        "binary_sensor.kuche_fenster_ikea_zustand",
    ]
)


def test_propose_mapping_exact_suffix():
    res = propose_mapping(OLD, NEW, {})
    pairs = {p["old_entity_id"]: p["new_entity_id"] for p in res["pairs"]}
    assert pairs["sensor.kuche_fenster_batterie"] == "sensor.kuche_fenster_ikea_batterie"
    assert pairs["binary_sensor.kuche_fenster_zustand"] == "binary_sensor.kuche_fenster_ikea_zustand"


def test_propose_mapping_no_match_stays_unmapped():
    # identifizieren vs taste -> different suffix -> not auto-mapped
    res = propose_mapping(OLD, NEW, {})
    assert "button.kuche_fenster_identifizieren" not in {p["old_entity_id"] for p in res["pairs"]}
    assert "button.kuche_fenster_identifizieren" in res["unmapped_old"]


def test_propose_mapping_in_use_filter():
    only = {"binary_sensor.kuche_fenster_zustand"}
    res = propose_mapping(OLD, NEW, {}, in_use_ids=only)
    assert [(p["old_entity_id"], p["new_entity_id"]) for p in res["pairs"]] == [
        ("binary_sensor.kuche_fenster_zustand", "binary_sensor.kuche_fenster_ikea_zustand")
    ]


# --------------------------------------------------------------------------- #
# _swap_friendly: prefix swap on the friendly name
# --------------------------------------------------------------------------- #


def _executor_with_states(states_by_id):
    ex = SwapExecutor.__new__(SwapExecutor)
    ex.states_by_id = states_by_id
    return ex


def test_swap_friendly_keeps_suffix():
    ex = _executor_with_states({"sensor.x": {"attributes": {"friendly_name": "Küche Fenster IKEA Batteriespannung"}}})
    assert ex._swap_friendly("sensor.x", "Küche Fenster IKEA", "Küche Fenster") == "Küche Fenster Batteriespannung"


def test_swap_friendly_no_prefix_match_returns_none():
    ex = _executor_with_states({"sensor.x": {"attributes": {"friendly_name": "Something Else"}}})
    assert ex._swap_friendly("sensor.x", "Küche Fenster IKEA", "Küche Fenster") is None


# --------------------------------------------------------------------------- #
# SwapExecutor.run: full flow with mock clients
# --------------------------------------------------------------------------- #


class _Rec:
    def __init__(self):
        self.calls = []


class _DR(_Rec):
    async def rename_device(self, dev, name):
        self.calls.append(("dev", dev, name))

    async def assign_area(self, dev, area_id):
        self.calls.append(("area", dev, area_id))


class _ER(_Rec):
    ws = object()

    def __init__(self):
        super().__init__()
        self.updates = []

    async def rename_entity(self, old, new, friendly=None):
        self.calls.append((old, new))

    async def update_entity(self, entity_id, **kwargs):
        self.updates.append((entity_id, kwargs))


class _DU(_Rec):
    async def update_all_dependencies(self, old, new, states=None):
        self.calls.append((old, new))


class _Bridge(_Rec):
    async def rename_native(self, device_data, new_name):
        from integration_bridge import BridgeResult

        return BridgeResult(success=True, native_supported=False, detail="n/a")

    async def remove_native(self, device_data, *, force=False):
        from integration_bridge import BridgeResult

        return BridgeResult(success=True, native_supported=True, detail="removed")


class _RS:
    async def load_structure(self, ws):
        pass


def _job():
    return {
        "job_id": "t1",
        "state": device_swap.STATE_CONFIRMED,
        "created": "t",
        "old_device": {"device_id": "old", "name": "Küche Fenster"},
        "new_device": {"device_id": "new", "name": "Küche Fenster IKEA"},
        "target_device_name": "Küche Fenster",
        "old_device_disposition": device_swap.DISPOSITION_KEEP,
        "old_device_entities": [
            "binary_sensor.kuche_fenster_zustand",
            "sensor.kuche_fenster_batterie",
        ],
        "new_device_entities": [
            "binary_sensor.kuche_fenster_ikea_zustand",
            "sensor.kuche_fenster_ikea_batterie",
            "sensor.kuche_fenster_ikea_batterietyp",
        ],
        "entity_mapping": [
            {
                "old_entity_id": "binary_sensor.kuche_fenster_zustand",
                "new_entity_id_current": "binary_sensor.kuche_fenster_ikea_zustand",
                "status": "pending",
            }
        ],
        "steps": {},
        "log": [],
    }


def _run(tmp_path, job):
    store = SwapJobStore(str(tmp_path))
    er = _ER()
    du = _DU()
    ex = SwapExecutor(store, _DR(), er, du, _Bridge(), _RS(), states_by_id={}, timestamp="t1")
    import asyncio

    out = asyncio.run(ex.run(job))
    return out, er, du


def test_executor_completes(tmp_path):
    out, _, _ = _run(tmp_path, _job())
    assert out["state"] == device_swap.STATE_COMPLETED


def test_executor_frees_old_entities(tmp_path):
    _, er, _ = _run(tmp_path, _job())
    freed = [c for c in er.calls if c[1].endswith("_swapout")]
    assert ("binary_sensor.kuche_fenster_zustand", "binary_sensor.kuche_fenster_zustand_swapout") in freed
    assert ("sensor.kuche_fenster_batterie", "sensor.kuche_fenster_batterie_swapout") in freed


def test_executor_renames_all_new_entities_prefix_swap(tmp_path):
    _, er, _ = _run(tmp_path, _job())
    # all three new entities incl. the unmapped batterietyp
    assert ("binary_sensor.kuche_fenster_ikea_zustand", "binary_sensor.kuche_fenster_zustand") in er.calls
    assert ("sensor.kuche_fenster_ikea_batterietyp", "sensor.kuche_fenster_batterietyp") in er.calls


def test_executor_rewires_deps_to_final_id(tmp_path):
    _, _, du = _run(tmp_path, _job())
    # only the in-use mapped pair, rewired to the final (suffix-preserved) id
    assert du.calls and du.calls[0] == (
        "binary_sensor.kuche_fenster_zustand",
        "binary_sensor.kuche_fenster_zustand",
    )


def test_executor_order_free_before_rename(tmp_path):
    _, er, _ = _run(tmp_path, _job())
    last_free = max(i for i, c in enumerate(er.calls) if c[1].endswith("_swapout"))
    first_rename = min(i for i, c in enumerate(er.calls) if "_ikea" in c[0])
    assert last_free < first_rename


def test_executor_idempotent_resume(tmp_path):
    # run once, then run the same (completed) job again -> no error, stays completed
    out, _, _ = _run(tmp_path, _job())
    import asyncio

    store = SwapJobStore(str(tmp_path))
    ex = SwapExecutor(store, _DR(), _ER(), _DU(), _Bridge(), _RS(), states_by_id={}, timestamp="t2")
    again = asyncio.run(ex.run(out))
    assert again["state"] == device_swap.STATE_COMPLETED


# --------------------------------------------------------------------------- #
# Area assignment + entity settings transfer
# --------------------------------------------------------------------------- #


def test_executor_assigns_old_area_to_new_device(tmp_path):
    job = _job()
    job["old_device"]["area_id"] = "bathroom"
    dr = _DR()
    store = SwapJobStore(str(tmp_path))
    ex = SwapExecutor(store, dr, _ER(), _DU(), _Bridge(), _RS(), states_by_id={}, timestamp="t1")
    import asyncio

    asyncio.run(ex.run(job))
    assert ("area", "new", "bathroom") in dr.calls


def _job_with_settings():
    job = _job()
    job["property_pairs"] = {
        "binary_sensor.kuche_fenster_zustand": "binary_sensor.kuche_fenster_ikea_zustand",
        "sensor.kuche_fenster_batterie": "sensor.kuche_fenster_ikea_batterie",
    }
    job["old_entity_props"] = {
        "binary_sensor.kuche_fenster_zustand": {
            "icon": "mdi:window",
            "area_id": "bathroom",
            "hidden_by": "user",
            "disabled_by": None,
            "entity_category": None,
            "device_class": "window",
        },
        "sensor.kuche_fenster_batterie": {
            "icon": None,
            "area_id": None,
            "hidden_by": None,
            "disabled_by": "user",
            "entity_category": "diagnostic",
            "device_class": None,
        },
    }
    return job


def test_executor_transfers_settings_to_final_ids(tmp_path):
    store = SwapJobStore(str(tmp_path))
    er = _ER()
    ex = SwapExecutor(store, _DR(), er, _DU(), _Bridge(), _RS(), states_by_id={}, timestamp="t1")
    import asyncio

    asyncio.run(ex.run(_job_with_settings()))
    updates = dict(er.updates)

    # binary_sensor: full copy incl. user-hidden, keyed by the final (suffix) id
    assert updates["binary_sensor.kuche_fenster_zustand"] == {
        "area_id": "bathroom",
        "icon": "mdi:window",
        "device_class": "window",
        "hidden_by": "user",
    }
    # sensor: area None (use device area), user-disabled + category; no icon/device_class
    assert updates["sensor.kuche_fenster_batterie"] == {
        "area_id": None,
        "entity_category": "diagnostic",
        "disabled_by": "user",
    }


def test_executor_settings_transfer_idempotent(tmp_path):
    store = SwapJobStore(str(tmp_path))
    job = _job_with_settings()
    ex = SwapExecutor(store, _DR(), _ER(), _DU(), _Bridge(), _RS(), states_by_id={}, timestamp="t1")
    import asyncio

    out = asyncio.run(ex.run(job))
    er2 = _ER()
    ex2 = SwapExecutor(store, _DR(), er2, _DU(), _Bridge(), _RS(), states_by_id={}, timestamp="t2")
    asyncio.run(ex2.run(out))
    # already transferred on the first run -> second run does not re-update
    assert er2.updates == []


# --------------------------------------------------------------------------- #
# SwapJobStore persistence
# --------------------------------------------------------------------------- #


def test_jobstore_save_load_list(tmp_path):
    store = SwapJobStore(str(tmp_path))
    store.save({"job_id": "abc", "state": device_swap.STATE_PROPOSED})
    assert store.load("abc")["state"] == device_swap.STATE_PROPOSED
    assert any(j["job_id"] == "abc" for j in store.list_unfinished())
    store.delete("abc")
    assert store.load("abc") is None
