"""Tests for the async rename_device endpoint (request-thread path only).

These verify that the request thread validates, sanitizes and enqueues the job
(and copies the payload, since the worker thread has no request context) without
touching Home Assistant. The worker is not started, so enqueued jobs stay queued.
"""

import asyncio

import pytest

from jobs import TERMINAL_STATES, JobContext, JobStore, JobWorker
import routes_entities
import web_ui


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = JobStore(str(tmp_path), terminal_states=TERMINAL_STATES)
    monkeypatch.setitem(web_ui.renamer_state, "job_store", store)
    monkeypatch.setitem(web_ui.renamer_state, "worker", JobWorker(store))
    web_ui.app.config["TESTING"] = True
    return web_ui.app.test_client(), store


def test_rename_device_enqueues_job(client):
    c, store = client
    resp = c.post("/api/rename_device", json={"device_id": "dev1", "new_name": "Kitchen Light"})
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["type"] == "rename_device"
    assert body["state"] == "queued"
    assert store.load(body["job_id"])["payload"] == {"device_id": "dev1", "new_name": "Kitchen Light"}


def test_rename_device_without_a_name_or_an_area_is_400(client):
    """A device id on its own asks for nothing: neither a name nor a move."""
    c, _ = client
    resp = c.post("/api/rename_device", json={"device_id": "dev1"})
    assert resp.status_code == 400


def test_rename_device_duplicate_device_is_409(client):
    c, _ = client
    assert c.post("/api/rename_device", json={"device_id": "dev1", "new_name": "A"}).status_code == 202
    dup = c.post("/api/rename_device", json={"device_id": "dev1", "new_name": "B"})
    assert dup.status_code == 409


def test_job_get_and_list(client):
    c, _ = client
    jid = c.post("/api/rename_device", json={"device_id": "dev1", "new_name": "A"}).get_json()["job_id"]
    assert c.get(f"/api/jobs/{jid}").get_json()["job_id"] == jid
    assert c.get("/api/jobs/does-not-exist").status_code == 404
    listing = c.get("/api/jobs").get_json()["jobs"]
    assert [j["job_id"] for j in listing] == [jid]


def test_device_rename_uses_active_naming_templates() -> None:
    """Device renames use the shared generator and retain entity-specific names."""

    class FakeRestructurer:
        """Provide the naming operations used by the device rename helpers."""

        entities = {
            "sensor.kitchen_sofa_energy": {"device_id": "device-1"},
            "sensor.kitchen_sofa_voltage": {"device_id": "device-1"},
            "sensor.unrelated": {"device_id": "device-2"},
        }
        last_resolutions: dict[str, dict[str, object]] = {}

        def build_naming_context(self, entity_id: str, state: dict) -> dict[str, str]:
            """Return the entity-specific part captured before the device rename."""
            return {"entity": state["attributes"]["native_name"]}

        def generate_new_entity_id(
            self,
            entity_id: str,
            state: dict,
            entity_name: str | None = None,
        ) -> tuple[str, str]:
            """Model a custom active template using the preserved entity name."""
            suffix = (entity_name or "").lower().replace(" ", "_")
            return f"sensor.ground_floor_sofa1_{suffix}", entity_name or ""

        def deduplicate_entity_ids(
            self,
            proposals: list[tuple[str, str, str]],
        ) -> list[tuple[str, str, str]]:
            """These targets are distinct, so hand them back untouched."""
            return list(proposals)

    states = [
        {"entity_id": "sensor.kitchen_sofa_energy", "attributes": {"native_name": "Energy"}},
        {"entity_id": "sensor.kitchen_sofa_voltage", "attributes": {"native_name": "Voltage"}},
    ]
    restructurer = FakeRestructurer()
    captured = routes_entities._capture_device_entity_naming(restructurer, "device-1", states)

    assert routes_entities._plan_device_entity_changes(restructurer, "device-1", states, captured) == [
        ("sensor.kitchen_sofa_energy", "sensor.ground_floor_sofa1_energy", "Energy"),
        ("sensor.kitchen_sofa_voltage", "sensor.ground_floor_sofa1_voltage", "Voltage"),
    ]


def test_a_device_rename_notes_the_type_part_it_used() -> None:
    """Otherwise the note says a name is ours without saying what went into it.

    The type part has to be read before the device is renamed - afterwards the
    device name can no longer be stripped off a name that froze it - so the
    same reading feeds the proposal and the note.
    """

    class FakeRestructurer:
        entities = {"sensor.kitchen_sofa_energy": {"device_id": "device-1"}}
        last_resolutions = {"sensor.kitchen_sofa_energy": {"won_by": "original", "rule_id": "rule-7"}}

        def build_naming_context(self, entity_id: str, state: dict) -> dict[str, str]:
            return {"entity": state["attributes"]["native_name"]}

    states = [{"entity_id": "sensor.kitchen_sofa_energy", "attributes": {"native_name": "Energy"}}]

    captured = routes_entities._capture_device_entity_naming(FakeRestructurer(), "device-1", states)

    assert captured["sensor.kitchen_sofa_energy"] == {
        "base_entity": "Energy",
        "won_by": "original",
        "rule_id": "rule-7",
    }


def test_init_client_recreates_missing_restructurer(monkeypatch) -> None:
    """A worker can restore the restructurer when the REST client already exists."""
    client = object()
    monkeypatch.setitem(web_ui.renamer_state, "client", client)
    monkeypatch.setitem(web_ui.renamer_state, "restructurer", None)

    assert asyncio.run(web_ui.init_client()) is client
    assert web_ui.renamer_state["restructurer"].client is client


class _NoWebSocket:
    """Stands in for the connection the handler opens and closes."""

    def __init__(self, url, token):
        pass

    async def connect(self):
        return None

    async def disconnect(self):
        return None


class _NoDependencies:
    def __init__(self, base_url, token):
        pass

    async def get_states(self):
        return []


class _NoRestructurer:
    entities: dict = {}

    async def load_structure(self, ws):
        return None


def _a_handler_that_cannot_rename(monkeypatch, registry):
    """Everything the handler reaches for, with the rename refused."""

    async def no_client():
        return None

    monkeypatch.setenv("HA_URL", "http://ha")
    monkeypatch.setenv("HA_TOKEN", "token")
    monkeypatch.setattr(routes_entities, "HomeAssistantWebSocket", _NoWebSocket)
    monkeypatch.setattr(routes_entities, "DeviceRegistry", lambda ws: registry)
    monkeypatch.setattr(routes_entities, "DependencyUpdater", _NoDependencies)
    monkeypatch.setattr(routes_entities, "init_client", no_client)
    monkeypatch.setitem(web_ui.renamer_state, "restructurer", _NoRestructurer())


def _run(tmp_path, job):
    store = JobStore(str(tmp_path), terminal_states=TERMINAL_STATES)
    store.save(job)
    context = JobContext(job, store)
    with pytest.raises(RuntimeError):
        asyncio.run(routes_entities.rename_device_handler(job, context))
    return [line["step"] for line in job.get("log", [])]


def test_a_move_that_went_through_is_logged_before_the_rename_can_fail(tmp_path, monkeypatch) -> None:
    """The area is written first, so a rename failing after it leaves the device
    somewhere it was not before. The interface says so, and it can only know
    from the log: the step is the contract between the two."""

    class Registry:
        moved_to = "unset"

        async def assign_area(self, device_id, area_id):
            Registry.moved_to = area_id

        async def rename_device(self, device_id, new_name):
            return False

    _a_handler_that_cannot_rename(monkeypatch, Registry())
    steps = _run(
        tmp_path,
        {
            "job_id": "j1",
            "type": "rename_device",
            "state": "running",
            "payload": {"device_id": "dev1", "new_name": "Bad Lampe", "area_id": "bad", "set_area": True},
        },
    )

    assert Registry.moved_to == "bad"
    assert "AREA" in steps, "the step a failure can be pinned on"
    assert "MOVED" in steps, "and the one that says the move stands"


def test_nothing_says_moved_where_no_area_was_asked_for(tmp_path, monkeypatch) -> None:
    """A rename alone leaves the device where it was, so the interface must not
    tell the user it was moved."""

    class Registry:
        async def assign_area(self, device_id, area_id):
            raise AssertionError("no area was asked for")

        async def rename_device(self, device_id, new_name):
            return False

    _a_handler_that_cannot_rename(monkeypatch, Registry())
    steps = _run(
        tmp_path,
        {
            "job_id": "j2",
            "type": "rename_device",
            "state": "running",
            "payload": {"device_id": "dev1", "new_name": "Bad Lampe"},
        },
    )

    assert "MOVED" not in steps
