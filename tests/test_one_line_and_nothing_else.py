"""Rewriting an entity id in a user's own YAML without damaging it.

The comments explaining why a threshold is 45 seconds, the blank lines grouping
three scripts, the anchors, the quoting, the order of keys: a repair that loses
any of it is not a repair. So the file is never parsed and written back - one
occurrence on one known line is replaced and every other byte stays as it was.

These tests hold that line. They compare the whole file, byte for byte, against
what it was.
"""

import os

import pytest

from yaml_edit import YamlEditor, _swap_in_line

# Every kind of thing a round trip through a parser would flatten.
CAREFULLY_WRITTEN = """\
# Balcony watering
#
# The 25s are under the 45s dry-run guard - do not raise without raising that.
script:

  prime_the_pump:
    alias: "Prime the pump"          # shown in the interface
    icon: mdi:pump
    mode: single
    sequence:
      - if:
          - condition: state
            entity_id: sensor.gone_away
            state: water_shortage
        then:
          - service: switch.turn_on
            target: { entity_id: switch.the_pump }

          # Let it run before closing again
          - delay: { seconds: 25 }

defaults: &defaults
  mode: single

another_script:
  <<: *defaults
  sequence: []
"""


@pytest.fixture
def config(tmp_path):
    """The user's configuration directory, with nothing of ours in it."""
    where = tmp_path / "config"
    where.mkdir()
    (where / "package.yaml").write_text(CAREFULLY_WRITTEN, encoding="utf-8")
    return where


@pytest.fixture
def editor(config, tmp_path):
    # The copies live in the add-on's own data directory, as they do in service.
    return YamlEditor(str(config), allowed=True, backup_dir=str(tmp_path / "data" / "yaml_backups"))


def read(path):
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def test_only_the_one_line_differs(editor, config):
    before = read(config / "package.yaml")

    outcome = editor.replace("package.yaml", 13, "sensor.gone_away", "sensor.came_back")

    assert outcome.changed
    after = read(config / "package.yaml")
    changed = [(one, two) for one, two in zip(before.splitlines(), after.splitlines()) if one != two]
    assert changed == [("            entity_id: sensor.gone_away", "            entity_id: sensor.came_back")]
    assert len(before.splitlines()) == len(after.splitlines())


def test_the_comments_survive(editor, config):
    editor.replace("package.yaml", 13, "sensor.gone_away", "sensor.came_back")
    after = read(config / "package.yaml")

    assert "# The 25s are under the 45s dry-run guard" in after
    assert "# shown in the interface" in after
    assert "# Let it run before closing again" in after


def test_the_anchors_and_blank_lines_survive(editor, config):
    editor.replace("package.yaml", 13, "sensor.gone_away", "sensor.came_back")
    after = read(config / "package.yaml")

    assert "defaults: &defaults" in after
    assert "<<: *defaults" in after
    assert "script:\n\n  prime_the_pump:" in after


def test_the_quoting_survives(editor, config):
    editor.replace("package.yaml", 13, "sensor.gone_away", "sensor.came_back")
    after = read(config / "package.yaml")

    assert 'alias: "Prime the pump"' in after
    assert "target: { entity_id: switch.the_pump }" in after


def test_windows_line_endings_are_kept(tmp_path):
    path = tmp_path / "crlf.yaml"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("script:\r\n  one:\r\n    entity_id: sensor.gone_away\r\n")
    editor = YamlEditor(str(tmp_path), allowed=True)

    assert editor.replace("crlf.yaml", 3, "sensor.gone_away", "sensor.came_back").changed
    assert read(path) == "script:\r\n  one:\r\n    entity_id: sensor.came_back\r\n"


def test_a_file_without_a_final_newline_keeps_not_having_one(tmp_path):
    path = tmp_path / "bare.yaml"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("entity_id: sensor.gone_away")
    editor = YamlEditor(str(tmp_path), allowed=True)

    editor.replace("bare.yaml", 1, "sensor.gone_away", "sensor.came_back")

    assert read(path) == "entity_id: sensor.came_back"


def test_a_longer_name_is_not_hit(tmp_path):
    """`sensor.a` must not match inside `sensor.a_b`."""
    path = tmp_path / "one.yaml"
    path.write_text("a: sensor.pump\nb: sensor.pump_two\n", encoding="utf-8")
    editor = YamlEditor(str(tmp_path), allowed=True)

    editor.replace("one.yaml", 1, "sensor.pump", "sensor.other")

    assert read(path) == "a: sensor.other\nb: sensor.pump_two\n"


def test_the_same_id_twice_on_a_line_is_replaced_twice(tmp_path):
    path = tmp_path / "one.yaml"
    path.write_text("a: [sensor.pump, sensor.pump]\n", encoding="utf-8")
    editor = YamlEditor(str(tmp_path), allowed=True)

    editor.replace("one.yaml", 1, "sensor.pump", "sensor.other")

    assert read(path) == "a: [sensor.other, sensor.other]\n"


# --- what it refuses to do ---------------------------------------------------


def test_without_permission_nothing_is_written(config, tmp_path):
    before = read(config / "package.yaml")
    editor = YamlEditor(str(config), allowed=False)

    outcome = editor.replace("package.yaml", 13, "sensor.gone_away", "sensor.came_back")

    assert not outcome.changed
    assert outcome.reason == "not_allowed"
    assert read(config / "package.yaml") == before


def test_a_dry_run_says_what_it_would_do_and_does_nothing(config):
    before = read(config / "package.yaml")
    editor = YamlEditor(str(config), allowed=False)

    outcome = editor.replace("package.yaml", 13, "sensor.gone_away", "sensor.came_back", dry_run=True)

    assert not outcome.changed
    assert outcome.reason == "dry_run"
    assert outcome.after.strip() == "entity_id: sensor.came_back"
    assert read(config / "package.yaml") == before


def test_a_line_that_has_moved_on_is_left_alone(editor, config):
    """The file may have been edited since it was read."""
    before = read(config / "package.yaml")

    outcome = editor.replace("package.yaml", 12, "sensor.gone_away", "sensor.came_back")

    assert not outcome.changed
    assert outcome.reason == "not_there"
    assert read(config / "package.yaml") == before


def test_a_path_leaving_the_configuration_directory_is_refused(tmp_path):
    outside = tmp_path / "outside.yaml"
    outside.write_text("a: sensor.gone_away\n", encoding="utf-8")
    inside = tmp_path / "config"
    inside.mkdir()
    editor = YamlEditor(str(inside), allowed=True)

    outcome = editor.replace("../outside.yaml", 1, "sensor.gone_away", "sensor.came_back")

    assert outcome.reason == "outside"
    assert read(outside) == "a: sensor.gone_away\n"


def test_a_change_that_would_break_the_file_is_refused(tmp_path):
    """Nothing is worth leaving Home Assistant unable to read its own file."""
    path = tmp_path / "one.yaml"
    path.write_text('a: "sensor.gone_away"\n', encoding="utf-8")
    before = read(path)
    editor = YamlEditor(str(tmp_path), allowed=True)

    outcome = editor.replace("one.yaml", 1, 'sensor.gone_away"', "broken\n  - [")

    assert not outcome.changed
    assert read(path) == before


def test_a_missing_file_is_said_rather_than_raised(editor):
    assert editor.replace("nowhere.yaml", 1, "a.b", "c.d").reason == "missing"


def test_a_line_past_the_end_is_said_rather_than_raised(editor):
    assert editor.replace("package.yaml", 9000, "a.b", "c.d").reason == "no_such_line"


# --- what it keeps -----------------------------------------------------------


def test_the_original_is_kept(editor, config, tmp_path):
    outcome = editor.replace("package.yaml", 13, "sensor.gone_away", "sensor.came_back")

    assert outcome.backup
    assert os.path.isfile(outcome.backup)
    assert read(outcome.backup) == CAREFULLY_WRITTEN


def test_the_copy_is_kept_out_of_the_users_directory(editor, config, tmp_path):
    outcome = editor.replace("package.yaml", 13, "sensor.gone_away", "sensor.came_back")

    assert not outcome.backup.startswith(str(config) + os.sep + "package")
    assert os.listdir(config) == ["package.yaml"]


def test_the_permissions_are_kept(editor, config):
    path = config / "package.yaml"
    os.chmod(path, 0o640)

    editor.replace("package.yaml", 13, "sensor.gone_away", "sensor.came_back")

    assert oct(os.stat(path).st_mode)[-3:] == "640"


def test_no_leftovers_beside_the_file(editor, config):
    editor.replace("package.yaml", 13, "sensor.gone_away", "sensor.came_back")

    assert [one for one in os.listdir(config) if one.startswith(".")] == []


def test_a_line_naming_nothing_leaves_the_id_alone():
    assert _swap_in_line("  a: something_else\n", "sensor.one", "sensor.two") is None


# --- the setting that gates it -----------------------------------------------


def test_writing_is_off_unless_asked_for(monkeypatch):
    import yaml_writing

    monkeypatch.delenv("FIX_YAML", raising=False)

    assert yaml_writing.mode() == "off"
    assert yaml_writing.allowed() is False


def test_the_beta_setting_turns_it_on(monkeypatch):
    import yaml_writing

    monkeypatch.setenv("FIX_YAML", "beta")

    assert yaml_writing.allowed() is True


def test_an_unknown_setting_writes_nothing(monkeypatch):
    """A typo in the setting must not be read as permission."""
    import yaml_writing

    monkeypatch.setenv("FIX_YAML", "yes please")

    assert yaml_writing.mode() == "off"
    assert yaml_writing.allowed() is False
