"""Tests for the device swap engine: mapping, prefix swap, friendly names, executor flow."""

import device_swap
from device_swap import (
    SwapExecutor,
    SwapJobStore,
    _common_prefix_tokens,
    _entity_name,
    propose_mapping,
)

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

    async def rename_entity(self, old, new, friendly=None):
        self.calls.append((old, new))


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
        "old_device": {"device_id": "old", "name": "Küche Fenster", "area_id": "kuche"},
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


def _run(tmp_path, job, device_registry=None):
    store = SwapJobStore(str(tmp_path))
    er = _ER()
    du = _DU()
    ex = SwapExecutor(store, device_registry or _DR(), er, du, _Bridge(), _RS(), states_by_id={}, timestamp="t1")
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


def test_executor_moves_the_new_device_into_the_old_area(tmp_path):
    """Der Bereich gehört zur Identität, die das neue Gerät übernimmt: dort
    hängen Bereichs-Automationen, Sprachsteuerung und die Namensgebung dran."""
    dr = _DR()

    out, _, _ = _run(tmp_path, _job(), device_registry=dr)

    assert ("area", "new", "kuche") in dr.calls
    assert out["assigned_area_id"] == "kuche"


def test_executor_assigns_the_area_before_the_entities_are_renamed(tmp_path):
    """Die Entity-Namen lesen den Bereich, also muss er vorher stehen."""
    dr = _DR()

    _, er, _ = _run(tmp_path, _job(), device_registry=dr)

    assigned = min(i for i, c in enumerate(dr.calls) if c[0] == "area")
    renamed = min(i for i, c in enumerate(dr.calls) if c[0] == "dev" and c[1] == "new")
    assert renamed < assigned
    assert any("_ikea" in call[0] for call in er.calls)


def test_executor_leaves_the_new_device_where_it_is_without_an_old_area(tmp_path):
    """Einen gesetzten Bereich zu entfernen nähme Information weg, statt
    welche zu übertragen."""
    job = _job()
    job["old_device"].pop("area_id")
    dr = _DR()

    out, _, _ = _run(tmp_path, job, device_registry=dr)

    assert not [c for c in dr.calls if c[0] == "area"]
    assert out["state"] == device_swap.STATE_COMPLETED


def test_a_job_from_before_this_step_still_gets_its_area(tmp_path):
    """Ein Job, der die alten Schritte schon hinter sich hat, holt den neuen
    beim Fortsetzen nach - sonst bliebe genau der Fall ungelöst, der den
    Fehler gemeldet hat."""
    job = _job()
    job["steps"] = {
        device_swap.STATE_FREEING_OLD_NAME: {"status": "done"},
        device_swap.STATE_RENAMING_NEW_DEVICE: {"status": "done"},
    }
    dr = _DR()

    _run(tmp_path, job, device_registry=dr)

    assert ("area", "new", "kuche") in dr.calls


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
