"""Tests for applying an area, a device name and the entities in one run.

The endpoint takes the area and the name separately and each of them on its
own, and the handler writes the area before the name, because the device name
and every entity name below it are built out of the area.
"""

import asyncio
import os
from typing import Any

import pytest

from jobs import TERMINAL_STATES, JobStore, JobWorker
import naming_service
import routes_entities
import web_ui

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = JobStore(str(tmp_path), terminal_states=TERMINAL_STATES)
    monkeypatch.setitem(web_ui.renamer_state, "job_store", store)
    monkeypatch.setitem(web_ui.renamer_state, "worker", JobWorker(store))
    web_ui.app.config["TESTING"] = True
    return web_ui.app.test_client(), store


def test_an_area_alone_is_enough_to_ask_for(client) -> None:
    """A device that only moves is renamed by its template, not by hand."""
    c, store = client
    resp = c.post("/api/rename_device", json={"device_id": "dev1", "area_id": "kitchen"})
    assert resp.status_code == 202
    assert store.load(resp.get_json()["job_id"])["payload"] == {
        "device_id": "dev1",
        "new_name": None,
        "area_id": "kitchen",
        "set_area": True,
    }


def test_an_area_and_a_name_travel_in_one_job(client) -> None:
    c, store = client
    resp = c.post(
        "/api/rename_device",
        json={"device_id": "dev1", "new_name": "Kitchen Plug", "area_id": "kitchen"},
    )
    assert store.load(resp.get_json()["job_id"])["payload"] == {
        "device_id": "dev1",
        "new_name": "Kitchen Plug",
        "area_id": "kitchen",
        "set_area": True,
    }


def test_a_null_area_clears_the_area(client) -> None:
    """Spelled out as null, and only then, the device is taken out of its area."""
    c, store = client
    resp = c.post("/api/rename_device", json={"device_id": "dev1", "area_id": None})
    assert resp.status_code == 202, resp.get_json()
    payload = store.load(resp.get_json()["job_id"])["payload"]
    assert payload["set_area"] is True
    assert payload["area_id"] is None


def test_an_unreadable_area_is_refused(client) -> None:
    c, _ = client
    assert c.post("/api/rename_device", json={"device_id": "dev1", "area_id": "../etc"}).status_code == 400


def test_neither_a_name_nor_an_area_is_refused(client) -> None:
    c, _ = client
    assert c.post("/api/rename_device", json={"device_id": "dev1"}).status_code == 400


class _Ctx:
    """The progress and log sink the worker hands a handler."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def progress(self, *args: Any, **kwargs: Any) -> None:
        pass

    def log(self, kind: str, line: str) -> None:
        self.lines.append((kind, line))


class _Registry:
    """A device registry that records the order it was written in."""

    def __init__(self, written: list[str]) -> None:
        self.written = written

    async def assign_area(self, device_id: str, area_id: str | None) -> None:
        self.written.append(f"area:{area_id}")

    async def rename_device(self, device_id: str, name: str) -> bool:
        self.written.append(f"name:{name}")
        return True


def _run(monkeypatch, payload: dict[str, Any], ctx: Any = None) -> tuple[list[str], dict[str, Any]]:
    """Run the handler against stand-ins for everything outside this module.

    Returns what was written to the registry, in order, and what the job
    reports back - which is what the log shows the user.
    """
    written: list[str] = []

    class Ws:
        async def connect(self) -> None:
            pass

        async def disconnect(self) -> None:
            pass

    class Restructurer:
        # On the instance, not on the class: a class-level dict is one dict for
        # every test that reaches this class, and what one test wrote into it
        # would still be there for the next.
        def __init__(self) -> None:
            self.entities: dict[str, dict[str, Any]] = {}
            self.last_resolutions: dict[str, dict[str, Any]] = {}

        async def load_structure(self, ws: Any) -> None:
            pass

        def deduplicate_entity_ids(self, proposals: Any) -> list[Any]:
            return list(proposals)

    class Updater:
        def __init__(self, *args: Any) -> None:
            pass

        async def get_states(self) -> list[dict[str, Any]]:
            return []

    class Templates:
        def fingerprint(self) -> str:
            return "hash"

    async def no_z2m(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"synced": False, "supported": False}

    async def no_client() -> None:
        return None

    monkeypatch.setenv("HA_URL", "http://ha.invalid")
    monkeypatch.setenv("HA_TOKEN", "token")
    monkeypatch.setattr(routes_entities, "HomeAssistantWebSocket", lambda *a, **k: Ws())
    monkeypatch.setattr(routes_entities, "init_client", no_client)
    monkeypatch.setattr(routes_entities, "DependencyUpdater", Updater)
    monkeypatch.setattr(routes_entities, "DeviceRegistry", lambda ws: _Registry(written))
    monkeypatch.setattr(routes_entities, "EntityRegistry", lambda ws: object())
    monkeypatch.setattr(routes_entities, "sync_z2m_name", no_z2m)
    monkeypatch.setitem(web_ui.renamer_state, "restructurer", Restructurer())
    monkeypatch.setitem(web_ui.renamer_state, "naming_templates", Templates())

    result = asyncio.run(routes_entities.rename_device_handler({"payload": payload}, ctx or _Ctx()))
    return written, result or {}


def _run_handler(monkeypatch, payload: dict[str, Any]) -> list[str]:
    """What the handler wrote to the device registry, in order."""
    return _run(monkeypatch, payload)[0]


def test_the_area_is_written_before_the_name(monkeypatch) -> None:
    """The name is built out of the area, so the area has to be there first."""
    written = _run_handler(
        monkeypatch,
        {"device_id": "dev1", "new_name": "Kitchen Plug", "area_id": "kitchen", "set_area": True},
    )
    assert written == ["area:kitchen", "name:Kitchen Plug"]


def test_a_move_without_a_new_name_renames_nothing(monkeypatch) -> None:
    written = _run_handler(monkeypatch, {"device_id": "dev1", "new_name": None, "area_id": "kitchen", "set_area": True})
    assert written == ["area:kitchen"]


def test_a_rename_without_an_area_leaves_the_area_alone(monkeypatch) -> None:
    written = _run_handler(monkeypatch, {"device_id": "dev1", "new_name": "Kitchen Plug"})
    assert written == ["name:Kitchen Plug"]


def test_a_move_and_a_rename_are_both_reported(monkeypatch) -> None:
    """The log is where a move is confirmed; naming only the rename hid it."""
    _, result = _run(
        monkeypatch,
        {"device_id": "dev1", "new_name": "Kitchen Plug", "area_id": "kitchen", "set_area": True},
    )
    assert result["message"] == "Device moved and renamed to: Kitchen Plug"


def test_a_rename_alone_says_so(monkeypatch) -> None:
    _, result = _run(monkeypatch, {"device_id": "dev1", "new_name": "Kitchen Plug"})
    assert result["message"] == "Device renamed to: Kitchen Plug"


def test_a_move_alone_says_so(monkeypatch) -> None:
    _, result = _run(
        monkeypatch,
        {"device_id": "dev1", "new_name": None, "area_id": "kitchen", "set_area": True},
    )
    assert result["message"] == "Device moved"


def test_the_area_step_is_logged_before_it_is_written(monkeypatch) -> None:
    """A write that raises left no step at all, so nothing said what failed."""
    ctx = _Ctx()
    _run(
        monkeypatch,
        {"device_id": "dev1", "new_name": None, "area_id": "kitchen", "set_area": True},
        ctx,
    )
    assert ("AREA", "dev1 -> kitchen") in ctx.lines


# --- One answer to "is an area move staged" --------------------------------
#
# The field that marks itself as holding a change, the apply button that counts
# what is staged and the run that writes it each worked it out for themselves,
# and they disagreed: an area deleted in Home Assistant while it stood staged
# here left the field blank and the button open on a move that could not be
# made. Clicking it said "nothing to apply", and there was no way to clear the
# staging but to turn to another device.


def _panel_source():
    with open(os.path.join(HERE, "templates", "index.html"), encoding="utf-8") as handle:
        return handle.read()


def test_only_one_place_asks_whether_the_area_is_still_there():
    """The guard against a deleted area lives in areaMoveStaged and nowhere else."""
    markup = _panel_source()

    assert "areaMoveStaged(picked, device)" in markup
    # The three that used to work it out now ask it.
    assert "return this.areaMoveStaged(this.devicePreviewAreaId, this.selectedDeviceData);" in markup
    assert "const areaStaged = this.areaMoveStaged(this.devicePreviewAreaId, device);" in markup
    assert "const areaStaged = this.areaMoveStaged(picked, device);" in markup
    # And the guard is in that one place: asked for once, by name.
    assert markup.count("this.hierarchy.areas.some(a => a.id === picked)") == 1


def test_a_pick_forgets_where_the_list_stood():
    """The field keeps the focus after Enter, and an arrow key pressed straight
    afterwards went on from the item just chosen rather than to the top."""
    markup = _panel_source()
    at = markup.index("pickArea(area) {")
    body = markup[at : markup.index("takeArea() {", at)]

    assert "this.areaComboAt = 0;" in body


def test_the_field_follows_the_areas_without_reading_what_it_writes():
    """areaOptions reads areaSearch, and the effect writes it: listed as a
    dependency, every pick and every panel change paid for a second run of the
    effect, and anything written there conditionally would have looped."""
    markup = _panel_source()
    at = markup.index('x-effect="devicePreviewAreaId')
    effect = markup[at : markup.index('"', at + 10)]

    assert "areaOptions" not in effect
    assert "panelAreaName()" in effect


def test_a_single_rename_notes_nothing_where_the_resolution_says_nothing() -> None:
    """ "" is the note for a name that has no type part at all. Written for "the
    resolution said neither an input nor a value", the next run read it as that
    answer and proposed stripping the type part off a name that has one.

    The whole-run path says None here; this is the entity renamed by itself.
    """

    class _Resolutions:
        last_resolutions = {"sensor.x": {"won_by": "ha", "input": None, "value": None}}

    web_ui.renamer_state["restructurer"] = _Resolutions()
    try:
        note = naming_service.provenance_for("sensor.x")
    finally:
        web_ui.renamer_state.pop("restructurer", None)

    assert note["base_entity"] is None


def test_the_field_stands_on_a_pick_only_while_it_is_a_move():
    """A pick of "no area" that Home Assistant has since carried out itself is
    not a move any more: the field said "No area" with no border to say it was
    staged, the apply button ignored it, and nothing could clear it."""
    markup = _panel_source()
    at = markup.index("panelAreaId() {")
    body = markup[at : markup.index("panelAreaName() {", at)]

    assert "this.devicePreviewAreaId === ''" not in body
    assert "if (this.deviceChangeStagedArea()) {" in body


def test_the_arrow_keys_start_where_the_highlight_is():
    """An area deleted in Home Assistant shortens the list without a keystroke,
    and stepping from a position past its end landed one item off what was lit."""
    markup = _panel_source()
    at = markup.index("comboStep(by, total, field) {")
    body = markup[at : at + 900]

    assert "const from = Math.min(this.areaComboAt, total - 1);" in body
    assert "this.areaComboAt = (from + by + total) % total;" in body


def test_the_clash_check_is_given_the_name_that_was_typed():
    """It is asked 300ms later. Turning to another device in between cleared the
    field, and the check then ran against the name the device already has - a
    real clash for the typed name went unnoticed."""
    markup = _panel_source()
    at = markup.index("const typedFor = selectedDeviceData;")
    body = markup[at : at + 300]

    assert "const typedName = $event.target.value;" in body
    assert "checkDeviceNameClash(typedFor, typedName)" in body


def test_the_area_list_is_keyed_on_when_the_registry_moved():
    """The list is read by the combo, by every item's class and by the empty
    line, so writing the areas out walked them several times per repaint."""
    markup = _panel_source()
    at = markup.index("get areaOptions() {")
    body = markup[at : markup.index("matchingAreas() {", at)]

    assert "this.hierarchyVersion," in body
    assert ".map(a => a.id" not in body


def test_an_empty_type_part_is_noted_as_one() -> None:
    """ "" is an answer - a name built out of area and device with no type part.
    Asked for with `or`, it read as no answer at all, and the note then said
    "nothing recorded" about a name whose type part is genuinely empty."""

    class _Resolutions:
        last_resolutions = {"sensor.x": {"won_by": "rule:user", "input": "", "value": "Licht"}}

    web_ui.renamer_state["restructurer"] = _Resolutions()
    try:
        note = naming_service.provenance_for("sensor.x")
    finally:
        web_ui.renamer_state.pop("restructurer", None)

    assert note["base_entity"] == ""
