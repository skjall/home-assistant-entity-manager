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

        def __init__(self) -> None:
            # One reading per instance. On the class they were shared, and a
            # second fake would have read the first one's answers.
            self.last_resolutions: dict[str, dict] = {}

        def build_naming_context(self, entity_id: str, state: dict) -> dict[str, str]:
            # The real resolver writes the reading down as it answers, and the
            # capture reads it from there; a fake that skips this would let a
            # missing "input" pass unnoticed.
            name = state["attributes"]["native_name"]
            self.last_resolutions[entity_id] = {
                "input": name,
                "value": name,
                "won_by": "original",
                "rule_id": "rule-7",
            }
            return {"entity": name}

    states = [{"entity_id": "sensor.kitchen_sofa_energy", "attributes": {"native_name": "Energy"}}]

    captured = routes_entities._capture_device_entity_naming(FakeRestructurer(), "device-1", states)

    assert captured["sensor.kitchen_sofa_energy"] == {
        "base_entity": "Energy",
        "type_part": "Energy",
        "won_by": "original",
        "rule_id": "rule-7",
    }


def test_a_device_rename_renders_what_the_rules_answered() -> None:
    """The note keeps the word that went in; the name gets the word that came out.

    They are the same until a rule changes it. Rendering the first one wrote
    the name past the rule that decides it: a contact sensor whose rule says
    "Zustand" came back out of a device rename called "Tuer", the word the
    integration supplies.
    """

    class FakeRestructurer:
        entities = {"binary_sensor.contact": {"device_id": "device-1"}}

        def __init__(self) -> None:
            # Its own, not the class's: a reading left on the class outlives
            # the test that wrote it.
            self.last_resolutions: dict[str, dict] = {}

        def build_naming_context(self, entity_id: str, state: dict) -> dict[str, str]:
            """What the rules answered, which is what a name is built from.

            The real one writes the reading down as it answers, and the capture
            reads it from there; a fake holding a reading it did not write
            would let the capture read a stale one and pass.
            """
            self.last_resolutions[entity_id] = {
                "input": "Tuer",
                "value": "Zustand",
                "won_by": "rule:user",
                "rule_id": "rule-9",
            }
            return {"entity": "Zustand"}

        def generate_new_entity_id(
            self,
            entity_id: str,
            state: dict,
            entity_name: str | None = None,
        ) -> tuple[str, str]:
            return "binary_sensor.store_contact_zustand", f"Store Contact {entity_name or ''}".strip()

        def deduplicate_entity_ids(
            self,
            proposals: list[tuple[str, str, str]],
        ) -> list[tuple[str, str, str]]:
            return list(proposals)

    states = [{"entity_id": "binary_sensor.contact", "attributes": {}}]
    restructurer = FakeRestructurer()

    captured = routes_entities._capture_device_entity_naming(restructurer, "device-1", states)

    # The note keeps what a rule matches on, so an edit still reaches it.
    assert captured["binary_sensor.contact"]["base_entity"] == "Tuer"
    assert captured["binary_sensor.contact"]["type_part"] == "Zustand"

    planned = routes_entities._plan_device_entity_changes(restructurer, "device-1", states, captured)

    assert planned == [("binary_sensor.contact", "binary_sensor.store_contact_zustand", "Store Contact Zustand")]


def test_init_client_recreates_missing_restructurer(monkeypatch) -> None:
    """A worker can restore the restructurer when the REST client already exists."""
    client = object()
    monkeypatch.setitem(web_ui.renamer_state, "client", client)
    monkeypatch.setitem(web_ui.renamer_state, "restructurer", None)

    assert asyncio.run(web_ui.init_client()) is client
    assert web_ui.renamer_state["restructurer"].client is client


def test_the_note_does_not_carry_the_type_part() -> None:
    """The type part is what the name is built from, not what a rule matches on.

    It is captured alongside the note and would be written with it if the
    filter were dropped, leaving a field in the registry that nothing reads.
    """
    reading = {
        "base_entity": "Tuer",
        "type_part": "Zustand",
        "won_by": "rule:user",
        "rule_id": "rule-9",
    }

    note = routes_entities._provenance_note(reading, "hash-1")

    assert note == {
        "base_entity": "Tuer",
        "won_by": "rule:user",
        "rule_id": "rule-9",
        "template_hash": "hash-1",
    }


def test_a_name_no_rule_decided_notes_no_rule() -> None:
    """None, not "": an empty string is an id, and a reader asking which rule
    named an entity would be told the same for a name no rule touched and for
    one whose rule has since been deleted. Every other way into the state file
    writes None here (naming_service.provenance_of)."""
    reading = {"base_entity": "Tuer", "type_part": "Tuer", "won_by": "original", "rule_id": None}

    assert routes_entities._provenance_note(reading, "hash-1")["rule_id"] is None


def test_a_name_nobody_recorded_a_type_part_for_notes_nothing() -> None:
    """None, not "": an empty string means "this name has no type part", which
    is an answer. Nothing recorded is not that answer, and reading it as one
    would have the next rename strip a type part the entity does carry."""
    reading = {"won_by": "rule:user", "rule_id": "rule-9"}

    assert routes_entities._provenance_note(reading, "hash-1")["base_entity"] is None
    assert routes_entities._provenance_note({"base_entity": ""}, "hash-1")["base_entity"] == ""


def test_a_bracket_that_tells_two_entities_apart_reaches_the_new_id() -> None:
    """Three uplink sensors resolve to one name; the bracket says which peer.

    The type part carries it, so the id says "(UP)" rather than being numbered
    away into _1 and _2 - which is what a name without the bracket would get.
    """

    class FakeRestructurer:
        entities = {
            "sensor.uplink_up": {"device_id": "device-1"},
            "sensor.uplink_down": {"device_id": "device-1"},
        }
        supplied = {
            "sensor.uplink_up": "PLC-Uplink PHY-Rate (UP)",
            "sensor.uplink_down": "PLC-Uplink PHY-Rate (DOWN)",
        }
        last_resolutions: dict[str, dict] = {}

        def build_naming_context(self, entity_id: str, state: dict) -> dict[str, str]:
            # The real resolver writes the reading down as it answers, bracket
            # and all, and the capture reads it from there.
            value = self.supplied[entity_id]
            self.last_resolutions[entity_id] = {
                "input": "PLC-Uplink PHY-Rate",
                "value": value,
                "won_by": "rule:system",
                "rule_id": None,
            }
            return {"entity": value}

        def generate_new_entity_id(
            self,
            entity_id: str,
            state: dict,
            entity_name: str | None = None,
        ) -> tuple[str, str]:
            slug = (entity_name or "").lower().replace("-", "_").replace(" ", "_")
            slug = slug.replace("(", "").replace(")", "")
            return f"sensor.hall_plc_{slug}", f"Hall PLC {entity_name or ''}".strip()

        def deduplicate_entity_ids(
            self,
            proposals: list[tuple[str, str, str]],
        ) -> list[tuple[str, str, str]]:
            return list(proposals)

    states = [
        {"entity_id": "sensor.uplink_up", "attributes": {}},
        {"entity_id": "sensor.uplink_down", "attributes": {}},
    ]
    restructurer = FakeRestructurer()

    captured = routes_entities._capture_device_entity_naming(restructurer, "device-1", states)
    planned = routes_entities._plan_device_entity_changes(restructurer, "device-1", states, captured)

    # The note keeps the word the rule matches on, which both of them share.
    assert captured["sensor.uplink_up"]["base_entity"] == "PLC-Uplink PHY-Rate"
    assert captured["sensor.uplink_up"]["won_by"] == "rule:system"
    assert [new_id for _, new_id, _ in planned] == [
        "sensor.hall_plc_plc_uplink_phy_rate_up",
        "sensor.hall_plc_plc_uplink_phy_rate_down",
    ]


def test_an_entity_with_no_reading_is_named_from_the_fresh_context() -> None:
    """One the registry reports only after the rename has nothing to hand over.

    It is planned like any other entity of the device; the type part is simply
    absent, and the name comes from the context built against the renamed
    device.
    """

    class FakeRestructurer:
        entities = {"sensor.late": {"device_id": "device-1"}}
        last_resolutions: dict[str, dict] = {}

        def build_naming_context(self, entity_id: str, state: dict) -> dict[str, str]:
            return {"entity": "Power"}

        def generate_new_entity_id(
            self,
            entity_id: str,
            state: dict,
            entity_name: str | None = None,
        ) -> tuple[str, str]:
            assert entity_name is None
            return "sensor.hall_meter_power", "Hall Meter Power"

        def deduplicate_entity_ids(
            self,
            proposals: list[tuple[str, str, str]],
        ) -> list[tuple[str, str, str]]:
            return list(proposals)

    planned = routes_entities._plan_device_entity_changes(FakeRestructurer(), "device-1", [], {})

    assert planned == [("sensor.late", "sensor.hall_meter_power", "Hall Meter Power")]
