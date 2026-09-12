"""Tests for the async rename_device endpoint (request-thread path only).

These verify that the request thread validates, sanitizes and enqueues the job
(and copies the payload, since the worker thread has no request context) without
touching Home Assistant. The worker is not started, so enqueued jobs stay queued.
"""

import asyncio

import pytest

from jobs import TERMINAL_STATES, JobStore, JobWorker
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


def test_rename_device_missing_field_is_400(client):
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
    names = routes_entities._capture_device_entity_names(restructurer, "device-1", states)

    assert routes_entities._plan_device_entity_changes(restructurer, "device-1", states, names) == [
        ("sensor.kitchen_sofa_energy", "sensor.ground_floor_sofa1_energy", "Energy"),
        ("sensor.kitchen_sofa_voltage", "sensor.ground_floor_sofa1_voltage", "Voltage"),
    ]


def test_init_client_recreates_missing_restructurer(monkeypatch) -> None:
    """A worker can restore the restructurer when the REST client already exists."""
    client = object()
    monkeypatch.setitem(web_ui.renamer_state, "client", client)
    monkeypatch.setitem(web_ui.renamer_state, "restructurer", None)

    assert asyncio.run(web_ui.init_client()) is client
    assert web_ui.renamer_state["restructurer"].client is client
