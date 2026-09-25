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
    # No "new_name" at all, rather than one that is null: a job carrying the key
    # reads to anything that asks whether it is there as a rename with nothing to
    # write.
    assert store.load(resp.get_json()["job_id"])["payload"] == {
        "device_id": "dev1",
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


# --- What the rows hold when the panel turns away -------------------------
#
# These read the page's own source. The staged previews are written by the
# browser, and what is held down here is which of them the page keeps: a name
# computed for a move that is not staged any more is a name nothing is going to
# write, and it sat on the rows of the device the panel had left.


def _panel_source():
    with open(os.path.join(HERE, "templates", "index.html"), encoding="utf-8") as handle:
        return handle.read()


def test_leaving_a_device_puts_its_rows_back():
    """Every panel switch says which device is being left, and the rows of that
    device are computed again from the registry."""
    markup = _panel_source()

    assert "dropStagedPreview(deviceId = null)" in markup
    # Read before it is written over, in all three switches.
    assert markup.count("const left = this.selectedDevice;") == 3
    assert markup.count("this.dropStagedPreview(left);") == 3
    at = markup.index("dropStagedPreview(deviceId = null)")
    body = markup[at : markup.index("previewDeviceWide() {", at)]
    assert "entity._previewName = null;" in body
    assert "this.computePreviewsFor(rows);" in body


def test_picking_an_area_asks_the_panel_for_the_names():
    """The computed previews are worked out from the registry, which knows
    nothing of a name typed into the field: the rows went back to the stored
    name at every pick, and only the next keystroke brought them forward."""
    markup = _panel_source()
    at = markup.index("pickPreviewArea(areaId) {")
    body = markup[at : markup.index("getDeviceDisplayName(device) {", at)]

    assert "this.previewDeviceWide();" in body


def test_the_row_renders_the_template_like_everything_else():
    """Joined with spaces, a template that puts the area last - or anything
    between the parts - had the row saying one name while the live answer and
    the server said another, and the id was made out of the row's answer."""
    markup = _panel_source()
    at = markup.index("getEntityFriendlyNameForEntity(entity) {")
    body = markup[at : markup.index("withoutLeading(text, prefix) {", at)]

    assert "renderNamingTemplate(" in body
    assert "parts.filter" not in body
    assert "join(' ')" not in body


def test_the_row_asks_for_the_id_once():
    """getNewEntityIdForEntity answers with the computed preview where there is
    one, so asking for that preview first said the same thing twice."""
    markup = _panel_source()

    assert "_previewId || getNewEntityIdForEntity" not in markup


def test_the_areas_are_looked_up_rather_than_walked():
    """Every name a row shows is built out of an area, and finding it walked the
    list once per row on every keystroke."""
    markup = _panel_source()
    at = markup.index("function indexed(state)")
    body = markup[at : markup.index("return {\n                // State", at)]

    assert "index.areasById.set(area.id, area);" in body
    # And the readings that used to walk the list ask the index.
    assert "indexed(this).areasById.get(areaId)" in markup


def test_a_row_is_not_applied_over_a_device_name_that_is_only_typed():
    """The row is named out of the field, so applying the row alone gave the
    entity a name saying a device name Home Assistant has never seen - and it
    stayed that way where the rename was never applied. Both are the device's own
    button to write, and it writes them in order."""
    markup = _panel_source()
    at = markup.index("async quickApplyEntity(entity) {")
    body = markup[at : markup.index("await this.previewsSettled();", at)]

    assert "this.t('messages.device_name_first')" in body
    # And the move is still written first, where only the area is staged: the row
    # is named out of it.
    assert body.index("device_name_first") < body.index("commitStagedArea")


def test_the_field_asks_for_one_answer_per_keystroke():
    """The staging waits 250ms of its own, so asking again from the clash timer
    sent a second slug request for every keystroke - the first of them always
    thrown away."""
    markup = _panel_source()
    at = markup.index('x-ref="deviceNameInput"')
    handler = markup[at : at + 900]

    assert handler.count("stageDeviceWideChange()") == 1


def test_the_background_refresh_asks_through_the_wait():
    """It runs on every poll of the list, and asking from there was a slug
    request per poll beside the ones the typing asks for."""
    markup = _panel_source()

    assert "if (stagedUnanswered) this.stageDeviceWideChange();" in markup
