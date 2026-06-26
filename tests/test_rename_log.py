"""Tests for the rename audit log: record, no-op guards, and chain resolution."""

from rename_log import RenameLog


def _log(tmp_path):
    return RenameLog(str(tmp_path / "rename_log.jsonl"))


def test_record_and_search_single_hop(tmp_path):
    log = _log(tmp_path)
    log.record("light.old", "light.new", "Kitchen Ceiling")

    result = log.search("light.old")
    assert result["found"] is True
    assert result["renamed"] is True
    assert result["current_entity_id"] == "light.new"
    assert len(result["history"]) == 1
    assert result["history"][0]["friendly_name"] == "Kitchen Ceiling"


def test_search_follows_rename_chain(tmp_path):
    log = _log(tmp_path)
    log.record("light.a", "light.b")
    log.record("light.b", "light.c")

    result = log.search("light.a")
    assert result["current_entity_id"] == "light.c"
    assert [h["old_entity_id"] for h in result["history"]] == ["light.a", "light.b"]


def test_search_unknown_entity_not_found(tmp_path):
    log = _log(tmp_path)
    log.record("light.a", "light.b")

    result = log.search("light.never_touched")
    assert result["found"] is False
    assert result["renamed"] is False


def test_record_skips_noop_and_missing(tmp_path):
    path = tmp_path / "rename_log.jsonl"
    log = RenameLog(str(path))

    log.record("light.same", "light.same")  # no actual id change
    log.record("", "light.new")  # missing old id
    log.record("light.old", "")  # missing new id

    assert not path.exists()
    assert log.search("light.same")["found"] is False


def test_latest_rename_wins_for_same_old_id(tmp_path):
    log = _log(tmp_path)
    log.record("light.a", "light.b")
    log.record("light.a", "light.c")  # re-renamed; latest should win

    result = log.search("light.a")
    assert result["current_entity_id"] == "light.c"


def test_search_handles_cycle_without_infinite_loop(tmp_path):
    log = _log(tmp_path)
    log.record("light.a", "light.b")
    log.record("light.b", "light.a")  # cycle a -> b -> a

    result = log.search("light.a")
    # Resolves through the recorded hops and stops instead of looping forever.
    assert result["found"] is True
    assert len(result["history"]) <= 2


def test_search_missing_file_returns_not_found(tmp_path):
    log = _log(tmp_path)
    result = log.search("light.anything")
    assert result == {"query": "light.anything", "found": False, "renamed": False}
