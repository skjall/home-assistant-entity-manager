"""Tests for applying an area, a device name and the entities in one run.

The endpoint takes the area and the name separately and each of them on its
own, and the handler writes the area before the name, because the device name
and every entity name below it are built out of the area.
"""

import asyncio
from typing import Any

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
