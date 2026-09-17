"""Reading the configuration files Home Assistant's API will not hand out.

A script defined in `packages/` answers 404 on `config/script/config/<id>`, so a
rename never reaches it and Spook later reports the dead reference. Home
Assistant itself sees the script - it parsed the YAML at startup - and so can we,
through the read-only mount. What we can say then is the one thing the user
needs: which file, which line, and what to put there instead.
"""

import pytest

from config_files import ConfigFiles

PACKAGE = """\
script:
  balkon_pumpe_priming:
    alias: "Balkon Pumpe anlaufen lassen"
    sequence:
      - if:
          - condition: state
            entity_id: sensor.balkon_ventil_geratestatus
            state: water_shortage
        then:
          - service: switch.turn_on
            target: { entity_id: switch.balkon_pumpe_zustand }
"""

DASHBOARD = """\
views:
  - cards:
      - type: entities
        entities:
          - entity: sensor.balkon_ventil_geratestatus
          - entity: sensor.bad_ventil_geratestatus
"""

MANAGED = """\
- id: '1234'
  alias: Written in the interface
  action:
    - service: switch.turn_on
      target: { entity_id: sensor.balkon_ventil_geratestatus }
"""


CONFIGURATION = """\
homeassistant:
  packages: !include_dir_named packages
lovelace:
  mode: yaml
automation: !include automations.yaml
"""


@pytest.fixture
def config(tmp_path):
    (tmp_path / "configuration.yaml").write_text(CONFIGURATION, encoding="utf-8")
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "balkon_wasser.yaml").write_text(PACKAGE, encoding="utf-8")
    (tmp_path / "ui-lovelace.yaml").write_text(DASHBOARD, encoding="utf-8")
    (tmp_path / "automations.yaml").write_text(MANAGED, encoding="utf-8")
    return ConfigFiles(str(tmp_path))


def test_a_missing_mount_is_not_an_error(tmp_path):
    """The add-on runs without the mount, and says so rather than failing."""
    absent = ConfigFiles(str(tmp_path / "nothing here"))

    assert absent.available() is False
    assert absent.files() == []
    assert absent.mentions("sensor.whatever") == []


def test_the_files_the_interface_writes_are_left_alone(config):
    """A rename is carried into automations.yaml through the API already."""
    assert "automations.yaml" not in config.files()
    assert "packages/balkon_wasser.yaml" in config.files()
    assert "ui-lovelace.yaml" in config.files()


def test_a_reference_is_found_with_its_file_and_line(config):
    found = config.mentions("sensor.balkon_ventil_geratestatus")
    where = sorted((one.path, one.line) for one in found)

    assert where == [("packages/balkon_wasser.yaml", 7), ("ui-lovelace.yaml", 5)]


def test_the_line_itself_comes_back_so_the_edit_can_be_named(config):
    found = config.mentions("sensor.balkon_ventil_geratestatus")
    package = [one for one in found if one.path.startswith("packages/")][0]

    assert package.text == "entity_id: sensor.balkon_ventil_geratestatus"


def test_a_longer_id_is_not_matched_by_a_shorter_one(config):
    """`sensor.bad_ventil_geratestatus` is not `sensor.bad_ventil`."""
    assert config.mentions("sensor.bad_ventil") == []
    assert len(config.mentions("sensor.bad_ventil_geratestatus")) == 1


def test_a_commented_out_line_is_not_a_reference(tmp_path):
    """Home Assistant does not read a comment, so nothing there can break."""
    (tmp_path / "configuration.yaml").write_text(CONFIGURATION, encoding="utf-8")
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "one.yaml").write_text(
        "# entity_id: sensor.old_one\nentity_id: sensor.old_one\n", encoding="utf-8"
    )
    found = ConfigFiles(str(tmp_path)).mentions("sensor.old_one")

    assert [one.line for one in found] == [2]


def test_a_comment_at_the_end_of_a_line_is_cut_off(tmp_path):
    """What stands before the comment is read; what stands after it is not."""
    (tmp_path / "configuration.yaml").write_text(CONFIGURATION, encoding="utf-8")
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "one.yaml").write_text(
        "entity_id: sensor.still_here  # was sensor.long_gone\n", encoding="utf-8"
    )
    files = ConfigFiles(str(tmp_path))

    assert len(files.mentions("sensor.still_here")) == 1
    assert files.mentions("sensor.long_gone") == []


def test_a_colour_is_not_a_comment(tmp_path):
    """`\'#ffc107\'` in a dashboard template has no space before the hash."""
    (tmp_path / "configuration.yaml").write_text(CONFIGURATION, encoding="utf-8")
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "one.yaml").write_text(
        "state: \"[[[ return x ? '#ffc107' : states['sensor.still_here'].state ]]]\"\n",
        encoding="utf-8",
    )

    assert len(ConfigFiles(str(tmp_path)).mentions("sensor.still_here")) == 1


def test_every_entity_named_anywhere_is_listed(config):
    found = config.every_mention()

    assert "sensor.balkon_ventil_geratestatus" in found
    assert "switch.balkon_pumpe_zustand" in found
    assert len(found["sensor.balkon_ventil_geratestatus"]) == 2


def test_only_the_domains_asked_for_are_listed(config):
    found = config.every_mention(domains={"sensor"})

    assert "switch.balkon_pumpe_zustand" not in found
    assert "sensor.balkon_ventil_geratestatus" in found


def test_the_storage_directory_is_never_read(tmp_path):
    """`.storage` is Home Assistant's own state, not configuration kept by hand."""
    (tmp_path / "configuration.yaml").write_text(CONFIGURATION, encoding="utf-8")
    (tmp_path / ".storage").mkdir()
    (tmp_path / ".storage" / "thing.yaml").write_text("entity_id: sensor.old_one\n", encoding="utf-8")

    assert ConfigFiles(str(tmp_path)).mentions("sensor.old_one") == []


def test_a_file_nothing_includes_is_not_part_of_the_configuration(tmp_path):
    """An old dashboard left in the directory is not what Home Assistant reads.

    Reporting its dead references would bury the ones that matter: one such file
    held 96 entities that no longer exist.
    """
    (tmp_path / "configuration.yaml").write_text(CONFIGURATION, encoding="utf-8")
    (tmp_path / "ui-lovelace.yaml").write_text(DASHBOARD, encoding="utf-8")
    (tmp_path / "ui-lovelace-full.yaml").write_text(DASHBOARD, encoding="utf-8")
    files = ConfigFiles(str(tmp_path))

    assert "ui-lovelace.yaml" in files.files()
    assert "ui-lovelace-full.yaml" not in files.files()


def test_a_dashboard_named_by_filename_is_read(tmp_path):
    """A second dashboard is named under `dashboards:`, not included."""
    (tmp_path / "configuration.yaml").write_text(
        "lovelace:\n  mode: yaml\n  dashboards:\n    second:\n      filename: second-board.yaml\n",
        encoding="utf-8",
    )
    (tmp_path / "second-board.yaml").write_text(DASHBOARD, encoding="utf-8")

    assert "second-board.yaml" in ConfigFiles(str(tmp_path)).files()


def test_a_service_call_is_not_an_entity(tmp_path):
    """`service: automation.turn_on` names a service Home Assistant provides."""
    (tmp_path / "configuration.yaml").write_text(CONFIGURATION, encoding="utf-8")
    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "one.yaml").write_text(
        "      - service: automation.turn_on\n        target: { entity_id: automation.wirklich_da }\n",
        encoding="utf-8",
    )
    found = ConfigFiles(str(tmp_path)).index()

    assert "automation.turn_on" not in found
    assert "automation.wirklich_da" in found


def test_configuration_yaml_decides_which_file_the_interface_writes(tmp_path):
    """`automation: !include my_automations.yaml` is editable in the interface."""
    (tmp_path / "configuration.yaml").write_text(
        "homeassistant:\n  name: Home\nautomation: !include my_automations.yaml\n", encoding="utf-8"
    )
    (tmp_path / "my_automations.yaml").write_text("- entity_id: sensor.old_one\n", encoding="utf-8")

    assert ConfigFiles(str(tmp_path)).mentions("sensor.old_one") == []


def test_an_included_directory_is_not_written_by_the_interface(tmp_path):
    """Home Assistant cannot write back into `!include_dir_merge_list`."""
    (tmp_path / "configuration.yaml").write_text("automation: !include_dir_merge_list automations/\n", encoding="utf-8")
    (tmp_path / "automations").mkdir()
    (tmp_path / "automations" / "one.yaml").write_text("- entity_id: sensor.old_one\n", encoding="utf-8")

    found = ConfigFiles(str(tmp_path)).mentions("sensor.old_one")

    assert [(one.path, one.line) for one in found] == [("automations/one.yaml", 1)]


def test_a_rename_says_which_line_to_edit(config):
    """What the user is left to do, spelled out per line."""
    from config_files import out_of_reach

    left = out_of_reach("sensor.balkon_ventil_geratestatus", "sensor.balkon_ventil_wasserstatus", config)

    assert [(one["path"], one["line"]) for one in left] == [
        ("/config/packages/balkon_wasser.yaml", 7),
        ("/config/ui-lovelace.yaml", 5),
    ]
    assert left[0]["replace"] == "sensor.balkon_ventil_geratestatus"
    assert left[0]["with"] == "sensor.balkon_ventil_wasserstatus"


def test_a_rename_that_keeps_the_id_leaves_nothing_to_do(config):
    """Only the id is written into other files; the friendly name is not."""
    from config_files import out_of_reach

    assert out_of_reach("sensor.balkon_ventil_geratestatus", "sensor.balkon_ventil_geratestatus", config) == []


def test_without_the_mount_nothing_is_claimed(tmp_path):
    """No mount means no knowledge, which is not the same as nothing to do."""
    from config_files import out_of_reach

    absent = ConfigFiles(str(tmp_path / "not mounted"))

    assert out_of_reach("sensor.a", "sensor.b", absent) == []


def test_the_path_is_shown_the_way_home_assistant_names_it(config):
    """The mount is at /homeassistant; the user knows the directory as /config."""
    assert config.shown_as("packages/balkon_wasser.yaml") == "/config/packages/balkon_wasser.yaml"
