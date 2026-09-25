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
    """As the route writes it: no "new_name" key at all. Passed as None, this
    covered a state nothing produces."""
    written = _run_handler(monkeypatch, {"device_id": "dev1", "area_id": "kitchen", "set_area": True})
    assert written == ["area:kitchen"]


def test_a_rename_asked_for_with_an_empty_name_is_refused(monkeypatch) -> None:
    """Asked for and empty is not the same as not asked for: a job carrying "" -
    a call straight to the API, or a job store somebody edited - had the rename
    skipped and the run reported as done."""
    with pytest.raises(RuntimeError, match="needs a name"):
        _run_handler(monkeypatch, {"device_id": "dev1", "new_name": "   "})


def test_a_name_that_is_not_a_string_is_refused_rather_than_skipped(monkeypatch) -> None:
    """0 and False are names nobody typed. Read by the truth test that writes the
    rename they were "no rename", so the job skipped it and reported success -
    while the key being there says a rename was asked for."""
    for name in (0, False):
        with pytest.raises(RuntimeError, match="needs a name"):
            _run_handler(monkeypatch, {"device_id": "dev1", "new_name": name})


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
    _, result = _run(monkeypatch, {"device_id": "dev1", "area_id": "kitchen", "set_area": True})
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
    """Two waits meant two requests: the rows asked at 250ms and the button beside
    them at 300ms, about the same ids, and the two answers had to agree to mean
    anything. One wait now, and the check reads what the rows were told."""
    markup = _panel_source()
    at = markup.index('x-ref="deviceNameInput"')
    handler = markup[at : at + 900]

    assert handler.count("setTimeout(") == 1
    assert "previewDeviceWide();" in handler
    assert "checkDeviceNameClash();" in handler

    at = markup.index("async checkDeviceNameClash(")
    body = markup[at : markup.index("const registry = indexed(this);", at)]
    assert "await this.previewsSettled();" in body
    assert "if (staged.every(Boolean)) ids = staged;" in body


def test_a_staged_preview_that_failed_does_not_go_unanswered():
    """Left to reject it was an unhandled rejection - no message, no fallback -
    and the page waited for an answer it had not been told about."""
    markup = _panel_source()
    at = markup.index("previewDeviceWide() {")
    body = markup[at : markup.index("entitiesOfDevice(", at)]

    assert "this._previewsInFlight.add(settling);" in body
    assert "console.error('Error computing staged previews:', error);" in body


def test_the_selected_devices_own_z2m_drift_is_applied_too():
    """The rename is what carries the Z2M name over, so a run that only moved the
    device left the drift standing - counted as a pending change, with nothing
    here able to resolve it."""
    markup = _panel_source()
    at = markup.index("const others = this.filteredDevices")
    body = markup[at - 900 : at + 300]

    assert "const renaming = !!typed" in body
    assert "(d.id !== this.selectedDevice || !renaming)" in body


def test_the_lookups_on_the_hot_paths_ask_the_index():
    """The two that are asked for every row on every keystroke, and the one the
    quick-apply of a row goes through. The lookups left walking the list run once
    after a write, where the list itself is what has to be read.
    """
    markup = _panel_source()

    at = markup.index("pendingDeviceContext(entity) {")
    assert "indexed(this).devicesById.get(entity.device_id)" in markup[at : at + 1500]

    at = markup.index("async commitStagedArea() {")
    assert "indexed(this).devicesById.get(deviceId)" in markup[at : at + 1200]

    # And the function the rows no longer call is gone rather than left to be
    # maintained as though it were live.
    assert "areaForDevice(" not in markup


def test_the_background_refresh_asks_through_the_wait():
    """It runs on every poll of the list, and asking from there was a slug
    request per poll beside the ones the typing asks for - and where one was
    already on its way, asking again called it off and left every staged row
    without an id until the second answer arrived."""
    markup = _panel_source()

    assert "if (stagedUnanswered && !this._stagedAsking) this.stageDeviceWideChange();" in markup
    at = markup.index("previewDeviceWide() {")
    body = markup[at : markup.index("entitiesOfDevice(", at)]
    assert "this._stagedAsking = true;" in body
    assert "if (run === this._stagedRun) this._stagedAsking = false;" in body


def test_a_typed_name_with_nowhere_to_go_is_said_out_loud_over_a_move():
    """The move was written, the run reported success, and the name the user had
    typed was dropped without a word - the device-name template has no {device} in
    it, so there was nowhere for it to go."""
    markup = _panel_source()
    at = markup.index("const payload = { device_id: deviceId };")
    body = markup[at : at + 2000]

    assert "if (payload.new_name === undefined && nameStaged && areaStaged) {" in body
    assert body.count("messages.name_has_no_place") == 2
    # And the field goes back to what it held, as it does where nothing at all
    # was written: left standing, it read as a rename waiting to be applied until
    # the job's own answer cleared it.
    assert body.count("this.devicePreviewBaseName = wasPreviewed;") == 2


def test_apply_all_goes_on_where_there_was_nothing_to_write():
    """A job renames the device and every entity under it, so there is nothing left
    for the batch. Where there was nothing for it to write - a name with nowhere to
    go - returning left the rest of the list unapplied."""
    markup = _panel_source()

    assert "if ((await this.applyDeviceWideChange(others)) !== 'nothing') return;" in markup
    at = markup.index("async applyDeviceWideChange(")
    body = markup[at : at + 1600]
    assert "if (outcome === 'running') return outcome;" in body


def test_a_start_that_was_refused_does_not_fall_through_to_the_batch():
    """A clash, or a run already going, says this device is not to be written. Read
    as "no job started", the batch below wrote the move and the names one by one -
    which is what the refusal was about."""
    markup = _panel_source()
    at = markup.index("async applyDeviceWideRun(")
    body = markup[at : markup.index("async syncZ2mName(", at)]

    # The device gone, a clash, a rename already under way, and the request that
    # threw: none of them is an answer the batch may go on from.
    assert body.count("return 'refused';") == 4
    assert body.count("return 'nothing';") == 1
    assert body.count("return 'running';") == 1
    # And nothing left over that reads as "no job started" to a caller.
    assert "\n                        return;\n" not in body

    at = markup.index("async applyDeviceWideChange(")
    body = markup[at : markup.index("async applyDeviceWideRun(", at)]
    assert "|| this.renamingDevice) return 'refused';" in body
    assert body.rstrip().endswith("return 'refused';\n                },")


def test_the_index_belongs_to_the_component_that_asked():
    """Two components mounted at once each count their own versions, and the second
    was handed the first one's devices wherever the two numbers agreed."""
    markup = _panel_source()
    at = markup.index("function indexed(state)")
    body = markup[at : at + 1200]

    assert "index.owner === state" in body
    assert "index.owner = state;" in body


def test_a_dropped_staged_answer_lets_the_next_one_be_asked_for(monkeypatch) -> None:
    """The answer dropped here leaves its own "finally" seeing a run that is not
    current any more, so it steps over the reset. Left standing, the flag said an
    answer was on its way for ever and nothing asked again: a row the poll
    delivered afterwards showed no id at all."""
    markup = _panel_source()
    at = markup.index("dropStagedPreview(deviceId = null) {")
    body = markup[at : markup.index("previewDeviceWide() {", at)]

    assert "this._stagedRun++;" in body
    assert "this._stagedAsking = false;" in body


def test_the_clash_check_reads_one_registry_for_both_sides(monkeypatch) -> None:
    """The rows were taken before the server answered. A poll arriving in between
    left the list holding rows the device does not have any more, compared against
    ids that no longer knew them."""
    markup = _panel_source()
    at = markup.index("async checkDeviceNameClash(")
    body = markup[at : markup.index("deviceNeedsRename(device)", at)]

    assert "const registry = indexed(this);" in body
    assert "const taken = registry.entitiesById;" in body
    assert "const stillMine = new Set((registry.byDevice.get(device.id) || none).map(e => e.id));" in body
    # A row that has gone is not renamed, so it is not asked about either.
    assert "if (!stillMine.has(entity.id)) return;" in body
    # One reading, not two: the second was the drift.
    assert body.count("indexed(this)") == 2


def test_the_apply_reads_what_the_rows_were_told(monkeypatch) -> None:
    """The apply always spells the name out, and reading that as "a name the rows
    know nothing about" asked the server the same question a second time on every
    apply - about the ids the rows already held."""
    markup = _panel_source()
    at = markup.index("async checkDeviceNameClash(")
    body = markup[at : markup.index("const registry = indexed(this);", at)]

    assert "const rowsHoldIt = baseName === null" in body
    assert "|| baseName === (this.devicePreviewBaseName === null ? null : this.devicePreviewBaseName.trim());" in body
    assert "if (rowsHoldIt && device.id === this.selectedDevice && this.deviceChangeStaged) {" in body


def test_an_area_asked_for_without_the_flag_is_still_written(monkeypatch) -> None:
    """The route writes both keys together, so a payload carrying an area and no
    flag is a call straight to the API or a job store edited by hand. Read as no
    move at all, the area was dropped and the run reported as done."""
    written = _run_handler(monkeypatch, {"device_id": "dev1", "area_id": "kitchen"})
    assert written == ["area:kitchen"]


def test_a_typed_name_is_read_for_the_device_it_was_typed_for() -> None:
    """The staged name belongs to the device the caller is asking about. Read off
    the panel whatever device that was, one device's unwritten name went into
    another device's rows - and into the clash check made from them."""
    markup = _panel_source()
    at = markup.index("entityNamingContext(entity, suffix = null")
    body = markup[at : markup.index("pendingDeviceContext(entity) {", at)]

    assert "entityNamingContext(entity, suffix = null, baseName = null, forDeviceId = null) {" in body
    assert "const staged = !!device && device.id === (forDeviceId || this.selectedDevice);" in body

    at = markup.index("getEntityFriendlyNameLive(entity, baseName = null, forDeviceId = null) {")
    body = markup[at : markup.index("getNewEntityIdLive(entity) {", at)]
    assert "this.entityNamingContext(entity, typed, mine ? baseName : null, forDeviceId)" in body


def test_the_staged_ids_are_an_answer_without_slugs_beside_them() -> None:
    """The rows' own ids are the answer, and there are no normalized names next to
    them. Read for a fallback, that threw where the fallback belongs - and the
    clash check stopped without saying anything."""
    markup = _panel_source()

    assert "answer.normalized[i]" not in markup
    assert "answer.normalized[index]" not in markup
    assert markup.count("(answer.normalized || [])[") == 2


def test_the_flag_for_an_answer_on_its_way_starts_out_false() -> None:
    """Read before anything set it, "not asking" and "never asked" were the same
    word: the guard let a second request go out that cancelled the one in flight."""
    markup = _panel_source()

    assert "_stagedAsking: false," in markup


def test_a_job_under_way_is_not_counted_as_a_change_to_apply() -> None:
    """The staged state it was started from stands until the job answers, and the
    job is already shown as running."""
    markup = _panel_source()
    at = markup.index("get pendingChangesCount() {")
    body = markup[at : markup.index("get disabledEntitiesCount() {", at)]

    assert "this.deviceChangeStaged && !this.renamingDevice ? 1 : 0" in body


def test_a_move_asked_for_without_an_area_is_refused(monkeypatch) -> None:
    """Null is "take it out of every area", which is a thing to ask for. The key
    missing is nobody asking, and read as null it took the device out of its area on
    a payload that said nothing about areas at all."""
    with pytest.raises(RuntimeError, match="needs an area"):
        _run_handler(monkeypatch, {"device_id": "dev1", "set_area": True})

    # Spelled out as null it is still a move, and the area is cleared.
    written = _run_handler(monkeypatch, {"device_id": "dev1", "set_area": True, "area_id": None})
    assert written == ["area:None"]


def test_the_name_is_built_for_the_area_this_run_writes() -> None:
    """Read off the panel again after the wait, a poll that cleared the staged area
    during the clash check had the name built for the area the device is leaving -
    while the move went to the one it was given."""
    markup = _panel_source()
    at = markup.index("async applyDeviceWideRun(")
    body = markup[at : markup.index("async syncZ2mName(", at)]

    assert "const newFullName = this.renderDeviceName(" in body
    assert "device, base, areaStaged ? (picked || null) : (device.area_id || null));" in body

    # And the area is the caller's to spell out, rather than read off the panel
    # wherever the name is rendered.
    at = markup.index("deviceNamingContext(device, baseName = null")
    body = markup[at : markup.index("entityNamingContext(entity, suffix = null", at)]
    assert "deviceNamingContext(device, baseName = null, areaId = undefined) {" in body
    assert "const wanted = areaId === undefined ? this.previewAreaFor(device) : areaId;" in body
    assert "renderDeviceName(device, baseName = null, areaId = undefined) {" in body
