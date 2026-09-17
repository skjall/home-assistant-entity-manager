"""Saying what a line in a configuration file belongs to.

A file and a line number tell the user where to go. They do not say what they
are looking at, and "packages/water.yaml:470" is a poor thing to hand someone
who is looking for a script they know by name.

The structure of the file answers it, and only the structure: no file name, no
language and no list of domains is assumed here, because installations lay their
YAML out however they like.
"""

import pytest

from yaml_structure import Structures

# `script:` holds a mapping of object ids. This is what a package looks like.
PACKAGE = """\
script:
  prime_the_pump:
    alias: "Prime the pump"
    sequence:
      - if:
          - condition: state
            entity_id: sensor.gone_away
            state: water_shortage
        then:
          - service: switch.turn_on
            target: { entity_id: switch.still_here }
"""

# `automation:` holds a list. Nothing names an object id.
LIST_OF_AUTOMATIONS = """\
- id: '1700000000000'
  alias: Water the balcony
  trigger:
    - platform: state
      entity_id: sensor.gone_away
  action:
    - service: switch.turn_on
"""

DASHBOARD = """\
views:
  - title: Ground floor
    cards:
      - type: entities
        entities:
          - entity: sensor.gone_away
"""

# A tag a plain parser refuses, and a helper that names itself with `name`.
WITH_TAGS = """\
homeassistant:
  customize: !include customize.yaml
input_boolean:
  holiday_mode:
    name: Holiday mode
    icon: mdi:palm-tree
template:
  - sensor:
      - name: Water left
        state: "{{ states('sensor.gone_away') }}"
"""


# What Home Assistant would recognise as a domain. An id is built only under one
# of these, so a dashboard's own mapping of names is not read as an entity.
DOMAINS = {"automation", "binary_sensor", "input_boolean", "light", "lock", "scene", "script", "sensor", "switch"}


@pytest.fixture
def files(tmp_path):
    (tmp_path / "package.yaml").write_text(PACKAGE, encoding="utf-8")
    (tmp_path / "automations.yaml").write_text(LIST_OF_AUTOMATIONS, encoding="utf-8")
    (tmp_path / "board.yaml").write_text(DASHBOARD, encoding="utf-8")
    (tmp_path / "tagged.yaml").write_text(WITH_TAGS, encoding="utf-8")
    return Structures(str(tmp_path), DOMAINS)


def test_a_script_in_a_package_is_named_and_identified(files):
    """The shape `script:` uses gives both an id and an alias."""
    where = files.what_holds("package.yaml", 7)

    assert where.object_id == "script.prime_the_pump"
    assert where.name == "Prime the pump"
    assert where.described() == "Prime the pump (script.prime_the_pump)"


def test_the_path_down_to_the_line_is_kept(files):
    where = files.what_holds("package.yaml", 7)

    assert where.trail[:2] == ["script", "prime_the_pump"]
    assert "sequence" in where.trail


def test_a_list_of_automations_names_no_id_but_does_name_itself(files):
    """`automation:` is a list, so no key states an object id."""
    where = files.what_holds("automations.yaml", 5)

    assert where.object_id is None
    assert where.name == "Water the balcony"
    assert where.described() == "Water the balcony"


def test_a_dashboard_answers_with_the_view(files):
    """A card is not an object; the view it sits in is the nearest thing named."""
    where = files.what_holds("board.yaml", 6)

    assert where.name == "Ground floor"
    assert where.object_id is None


def test_a_tag_no_plain_parser_knows_does_not_stop_the_read(files):
    """`!include` is read as an ordinary value, so the rest of the file still parses."""
    where = files.what_holds("tagged.yaml", 6)

    assert where.object_id == "input_boolean.holiday_mode"
    assert where.name == "Holiday mode"


def test_a_template_entity_is_named_without_an_id(files):
    """`template:` nests a list inside a list; only the name is there to give."""
    where = files.what_holds("tagged.yaml", 11)

    assert where.name == "Water left"
    assert where.object_id is None


def test_a_file_that_does_not_parse_says_nothing_rather_than_guessing(tmp_path):
    (tmp_path / "broken.yaml").write_text("script:\n  one:\n   - a\n  - b\n", encoding="utf-8")

    assert Structures(str(tmp_path)).what_holds("broken.yaml", 3) is None


def test_a_file_that_is_not_there_says_nothing(tmp_path):
    assert Structures(str(tmp_path)).what_holds("absent.yaml", 1) is None


def test_a_line_past_the_end_says_nothing(files):
    assert files.what_holds("package.yaml", 9000) is None


def test_the_file_is_parsed_once_however_many_lines_are_asked_about(files, monkeypatch):
    """A rename asks about many lines of the same file."""
    import yaml_structure

    reads = []
    real = yaml_structure.yaml.compose

    def counted(*args, **kwargs):
        reads.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(yaml_structure.yaml, "compose", counted)
    files.what_holds("package.yaml", 7)
    files.what_holds("package.yaml", 3)
    files.what_holds("package.yaml", 11)

    assert len(reads) == 1


def test_a_path_through_nothing_named_is_cut_short(tmp_path):
    """A dashboard nests deep, and the whole way down reads as noise."""
    (tmp_path / "deep.yaml").write_text(
        "views:\n"
        "  - cards:\n"
        "      - type: vertical-stack\n"
        "        cards:\n"
        "          - type: entities\n"
        "            entities:\n"
        "              - entity: sensor.gone_away\n",
        encoding="utf-8",
    )
    where = Structures(str(tmp_path)).what_holds("deep.yaml", 7)

    assert where.name is None
    assert where.described().endswith("→ …")
    assert where.described().count("→") == 3
    assert where.trail[0] == "views"


def test_a_mapping_of_names_under_some_other_key_is_not_an_entity(tmp_path):
    """A dashboard keeps its card templates as a mapping like any other.

    Reading `button_card_templates: stat_card: ...` as an entity id said
    something untrue, and the user went looking for an entity that never was.
    """
    (tmp_path / "board.yaml").write_text(
        "button_card_templates:\n"
        "  stat_card:\n"
        "    show_state: true\n"
        "    state:\n"
        "      - value: sensor.gone_away\n",
        encoding="utf-8",
    )
    where = Structures(str(tmp_path), DOMAINS).what_holds("board.yaml", 5)

    assert where.object_id is None


def test_without_a_list_of_domains_no_id_is_claimed(tmp_path):
    """A wrong id is worse than none: it sends the user after something absent."""
    (tmp_path / "package.yaml").write_text(PACKAGE, encoding="utf-8")

    where = Structures(str(tmp_path)).what_holds("package.yaml", 7)

    assert where.object_id is None
    assert where.name == "Prime the pump"
