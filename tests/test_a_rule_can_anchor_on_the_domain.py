"""A rule that recognises an entity by its domain.

Every other anchor says what an entity measures: its integration's translation
key, the canonical form of the name it supplies, its device class. Some
entities have none of those. UniFi names each of its device trackers after the
client it found, so fourteen of them carry fourteen different names, no key and
no class. There is no anchor they share except being device trackers, and
without one there is no rule that can reach more than one of them.

The domain is that anchor, and it is the last of them: anything that says what
an entity measures decides before it does.
"""

import pytest

from entity_restructurer import EntityRestructurer
from naming_overrides import NamingOverrides
from naming_rules import NamingRules
from naming_templates import NamingTemplates
import routes_naming
from type_mappings import DEFAULT_SYSTEM_MAPPINGS, TypeMappings
import web_ui

# Taken from a real installation: no two of them are alike.
SUPPLIED = {
    "device_tracker.unifi_default_00_70_07_24_e6_38": "00:70:07:24:e6:38 Dusche Bluetooth Proxy",
    "device_tracker.unifi_default_de_91_e5_f7_12_73": "iPhone",
    "device_tracker.unifi_default_6a_0b_15_00_60_ba": "Watch",
    "device_tracker.unifi_default_1c_af_4a_c7_5d_b1": "JanMacBook-Pro",
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A handful of UniFi device trackers, one from elsewhere, and a sensor."""
    rules = NamingRules(
        str(tmp_path / "naming_rules.json"),
        device_class_keys=DEFAULT_SYSTEM_MAPPINGS["device_class"].keys(),
        default_language="de",
    )
    mappings = TypeMappings(user_mappings_path=str(tmp_path / "unused.json"), rules=rules)
    overrides = NamingOverrides(str(tmp_path / "overrides.json"))
    restructurer = EntityRestructurer(
        client=object(),
        naming_overrides=overrides,
        type_mappings=mappings,
        naming_templates=NamingTemplates(str(tmp_path / "templates.json")),
    )
    restructurer.naming_templates.apply_preset("entity_manager")
    restructurer.floors = {}
    restructurer.areas = {"d": {"area_id": "d", "name": "Dusche"}}
    restructurer.devices = {"ap": {"id": "ap", "name": "Access Point", "area_id": "d", "model": "U6"}}
    restructurer.entities = {
        entity_id: {
            "id": f"reg-{index}",
            "entity_id": entity_id,
            "device_id": "ap",
            "platform": "unifi",
            "original_name": supplied,
            "has_entity_name": False,
        }
        for index, (entity_id, supplied) in enumerate(SUPPLIED.items())
    }
    # A device tracker from somewhere else, so a rule scoped to UniFi can be
    # shown to stop at its edge.
    restructurer.entities["device_tracker.jans_iphone"] = {
        "id": "reg-mobile",
        "entity_id": "device_tracker.jans_iphone",
        "device_id": "ap",
        "platform": "mobile_app",
        "original_name": "Jans iPhone",
        "has_entity_name": False,
    }
    # And something that is not a device tracker at all.
    restructurer.entities["sensor.ap_durchsatz"] = {
        "id": "reg-sensor",
        "entity_id": "sensor.ap_durchsatz",
        "device_id": "ap",
        "platform": "unifi",
        "original_name": "Durchsatz",
        "has_entity_name": False,
    }

    monkeypatch.setitem(web_ui.renamer_state, "naming_rules", rules)
    monkeypatch.setitem(web_ui.renamer_state, "type_mappings", mappings)
    monkeypatch.setitem(web_ui.renamer_state, "naming_overrides", overrides)
    monkeypatch.setitem(web_ui.renamer_state, "restructurer", restructurer)
    web_ui.app.config["TESTING"] = True
    return web_ui.app.test_client(), restructurer, rules


def name_of(restructurer, entity_id):
    return restructurer.generate_new_entity_id(entity_id, restructurer.entities[entity_id])[1]


# --- What was missing -----------------------------------------------------


def test_without_the_domain_anchor_a_rule_reaches_one_of_them(home):
    """Their supplied names have nothing in common, so the anchor taken by
    itself - the name - matches only the entity it was learned from."""
    client, _, _ = home

    answer = client.post(
        "/api/naming/learn",
        json={"entity_id": "device_tracker.unifi_default_de_91_e5_f7_12_73", "value": "Standort"},
    )

    rule = answer.get_json()["rule"]
    assert rule["match"]["kind"] == "name"
    assert rule["affected"] == 1


# --- The anchor -----------------------------------------------------------


def test_the_domain_anchor_reaches_every_device_tracker_of_the_integration(home):
    client, restructurer, _ = home

    answer = client.post(
        "/api/naming/learn",
        json={
            "entity_id": "device_tracker.unifi_default_de_91_e5_f7_12_73",
            "value": "Standort",
            "anchor": "domain",
            "scope": "integration",
        },
    )

    rule = answer.get_json()["rule"]
    assert rule["match"] == {"kind": "domain", "value": "device_tracker"}
    assert rule["filters"] == [{"integration": "unifi"}]
    for entity_id in SUPPLIED:
        assert name_of(restructurer, entity_id) == "Dusche Access Point Standort"


def test_the_rule_stops_at_the_integration_it_was_scoped_to(home):
    client, restructurer, _ = home

    client.post(
        "/api/naming/learn",
        json={
            "entity_id": "device_tracker.unifi_default_de_91_e5_f7_12_73",
            "value": "Standort",
            "anchor": "domain",
            "scope": "integration",
        },
    )

    assert "Standort" not in name_of(restructurer, "device_tracker.jans_iphone")


def test_the_rule_stops_at_its_domain(home):
    """It says what kind of thing an entity is, not what it measures."""
    client, restructurer, _ = home

    client.post(
        "/api/naming/learn",
        json={
            "entity_id": "device_tracker.unifi_default_de_91_e5_f7_12_73",
            "value": "Standort",
            "anchor": "domain",
            "scope": "integration",
        },
    )

    assert "Standort" not in name_of(restructurer, "sensor.ap_durchsatz")


def test_everywhere_takes_no_filter_at_all(home):
    client, restructurer, _ = home

    answer = client.post(
        "/api/naming/learn",
        json={
            "entity_id": "device_tracker.unifi_default_de_91_e5_f7_12_73",
            "value": "Standort",
            "anchor": "domain",
            "scope": "global",
        },
    )

    assert answer.get_json()["rule"]["filters"] == []
    assert name_of(restructurer, "device_tracker.jans_iphone") == "Dusche Access Point Standort"


def test_a_name_rule_still_decides_before_the_domain(home):
    """The domain says the least of any anchor, so it decides last."""
    client, restructurer, _ = home
    client.post(
        "/api/naming/learn",
        json={
            "entity_id": "device_tracker.unifi_default_de_91_e5_f7_12_73",
            "value": "Standort",
            "anchor": "domain",
            "scope": "integration",
        },
    )

    client.post(
        "/api/naming/learn",
        json={"entity_id": "device_tracker.unifi_default_1c_af_4a_c7_5d_b1", "value": "Laptop"},
    )

    assert name_of(restructurer, "device_tracker.unifi_default_1c_af_4a_c7_5d_b1") == "Dusche Access Point Laptop"
    assert name_of(restructurer, "device_tracker.unifi_default_6a_0b_15_00_60_ba") == "Dusche Access Point Standort"


def test_an_anchor_nobody_knows_is_refused(home):
    client, _, _ = home

    answer = client.post(
        "/api/naming/learn",
        json={"entity_id": "device_tracker.jans_iphone", "value": "Standort", "anchor": "geraeteklasse"},
    )

    assert answer.status_code == 400


# --- How far it reaches ---------------------------------------------------


def test_the_counts_say_how_far_it_would_reach(home):
    """Nothing else warns: a domain rule on `sensor` would catch hundreds."""
    _, restructurer, _ = home

    assert routes_naming.domain_counts(restructurer) == {"device_tracker": 5, "sensor": 1}
    assert routes_naming.domain_integration_counts(restructurer) == {
        ("device_tracker", "unifi"): 4,
        ("device_tracker", "mobile_app"): 1,
        ("sensor", "unifi"): 1,
    }
