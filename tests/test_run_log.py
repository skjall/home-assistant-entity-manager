"""The log of past runs, searchable and paged.

A run's log used to exist only inside the panel that was open while it ran.
These tests pin what the settings page reads instead: every line every finished
run wrote, newest first, narrowed by what happened and by the kind of run.
"""

import pytest

from jobs import TERMINAL_STATES, JobStore
import web_ui


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = JobStore(str(tmp_path / "jobs"), terminal_states=TERMINAL_STATES)
    store.save(
        {
            "job_id": "one",
            "type": "execute_direct",
            "state": "completed",
            "created": "2026-09-16T10:00:00+00:00",
            "log": [
                {"ts": "2026-09-16T10:00:01+00:00", "step": "RENAME", "message": "sensor.a -> sensor.b"},
                {"ts": "2026-09-16T10:00:02+00:00", "step": "UNREACHABLE", "message": "automation.x nennt sensor.a"},
            ],
        }
    )
    store.save(
        {
            "job_id": "two",
            "type": "enable_all",
            "state": "completed",
            "created": "2026-09-15T09:00:00+00:00",
            "log": [{"ts": "2026-09-15T09:00:01+00:00", "step": "ENABLE", "message": "light.c aktiviert"}],
        }
    )
    monkeypatch.setitem(web_ui.renamer_state, "job_store", store)
    web_ui.app.config["TESTING"] = True
    return web_ui.app.test_client()


def test_every_run_is_in_one_list_newest_first(client):
    answer = client.get("/api/log").get_json()

    assert answer["total"] == 3
    assert [line["step"] for line in answer["entries"]] == ["UNREACHABLE", "RENAME", "ENABLE"]
    assert answer["entries"][0]["job_type"] == "execute_direct"


def test_the_search_looks_at_the_message(client):
    answer = client.get("/api/log?q=automation.x").get_json()

    assert answer["total"] == 1
    assert answer["entries"][0]["step"] == "UNREACHABLE"


def test_a_step_can_be_kept_and_the_rest_dropped(client):
    answer = client.get("/api/log?step=RENAME").get_json()

    assert [line["step"] for line in answer["entries"]] == ["RENAME"]


def test_several_steps_widen_rather_than_cancel(client):
    answer = client.get("/api/log?step=RENAME,ENABLE").get_json()

    assert {line["step"] for line in answer["entries"]} == {"RENAME", "ENABLE"}


def test_a_kind_of_run_can_be_kept(client):
    answer = client.get("/api/log?type=enable_all").get_json()

    assert answer["total"] == 1
    assert answer["entries"][0]["job_type"] == "enable_all"


def test_the_counts_are_taken_before_the_step_filter(client):
    """Otherwise picking one step would answer that every other one has none."""
    answer = client.get("/api/log?step=RENAME").get_json()

    assert answer["facets"]["step"] == {"RENAME": 1, "UNREACHABLE": 1, "ENABLE": 1}


def test_the_list_is_answered_one_page_at_a_time(client):
    first = client.get("/api/log?per_page=2").get_json()
    second = client.get("/api/log?per_page=2&page=2").get_json()

    assert first["pages"] == 2
    assert len(first["entries"]) == 2
    assert len(second["entries"]) == 1
    assert second["entries"][0]["step"] == "ENABLE"


def test_a_page_past_the_end_answers_the_last_one(client):
    answer = client.get("/api/log?per_page=2&page=99").get_json()

    assert answer["page"] == 2


def test_a_page_size_nobody_should_ask_for_is_capped(client):
    answer = client.get("/api/log?per_page=10000").get_json()

    assert answer["per_page"] == 200


def test_nonsense_paging_is_answered_rather_than_refused(client):
    answer = client.get("/api/log?page=x&per_page=y").get_json()

    assert answer["page"] == 1
    assert answer["per_page"] == 50
