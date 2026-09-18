"""A rule may say which domain it means.

`ecoflow_cloud` supplies "Custom Load Power" twice over: a sensor for what the
load draws and a number for what it is set to. One name, two entities, and
until now one rule could only reach both or neither - the alternative being an
exception per entity, which the next device of the same model starts again.
"""

import pytest

from naming_rules import NamingRuleError, NamingRules, clean_filter, filter_rank


@pytest.fixture
def rules(tmp_path):
    return NamingRules(str(tmp_path / "rules.json"), default_language="de")


def test_a_filter_may_name_a_domain():
    assert clean_filter({"integration": "ecoflow_cloud", "domain": "sensor"}) == {
        "integration": "ecoflow_cloud",
        "domain": "sensor",
    }


def test_a_domain_is_read_as_home_assistant_writes_it():
    assert clean_filter({"domain": " Sensor "}) == {"domain": "sensor"}


def test_one_entity_and_a_domain_are_not_both():
    with pytest.raises(NamingRuleError):
        clean_filter({"registry_id": "registry-1", "domain": "sensor"})


def test_a_domain_narrows_what_it_is_added_to():
    everywhere = filter_rank({})
    integration = filter_rank({"integration": "ecoflow_cloud"})
    with_domain = filter_rank({"integration": "ecoflow_cloud", "domain": "sensor"})
    model = filter_rank({"model": "Delta 2"})

    assert with_domain < integration < everywhere
    assert filter_rank({"model": "Delta 2", "domain": "sensor"}) < model


def test_the_sensor_and_the_number_get_their_own_name(rules):
    rules.upsert("name", "Custom Load Power", "ecoflow_cloud", "de", "Andere Lasten", domain="sensor")
    rules.upsert("name", "Custom Load Power", "ecoflow_cloud", "de", "Energieverbrauch fix", domain="number")

    as_sensor = rules.find("name", "Custom Load Power", "ecoflow_cloud", "de", None, "sensor")
    as_number = rules.find("name", "Custom Load Power", "ecoflow_cloud", "de", None, "number")

    assert as_sensor["targets"]["de"] == "Andere Lasten"
    assert as_number["targets"]["de"] == "Energieverbrauch fix"


def test_a_rule_with_a_domain_is_not_found_for_another_one(rules):
    rules.upsert("name", "Custom Load Power", "ecoflow_cloud", "de", "Andere Lasten", domain="sensor")

    assert rules.find("name", "Custom Load Power", "ecoflow_cloud", "de", None, "number") is None


def test_a_rule_without_a_domain_still_answers_for_every_domain(rules):
    """Everything written before this existed says nothing about a domain."""
    rules.upsert("name", "Battery Level", "ecoflow_cloud", "de", "Ladestand")

    for domain in ("sensor", "number", "switch"):
        found = rules.find("name", "Battery Level", "ecoflow_cloud", "de", None, domain)
        assert found["targets"]["de"] == "Ladestand"


def test_the_narrower_rule_wins(rules):
    rules.upsert("name", "Custom Load Power", "ecoflow_cloud", "de", "Last")
    rules.upsert("name", "Custom Load Power", "ecoflow_cloud", "de", "Andere Lasten", domain="sensor")

    as_sensor = rules.find("name", "Custom Load Power", "ecoflow_cloud", "de", None, "sensor")
    as_number = rules.find("name", "Custom Load Power", "ecoflow_cloud", "de", None, "number")

    assert as_sensor["targets"]["de"] == "Andere Lasten"
    assert as_number["targets"]["de"] == "Last"


def test_why_says_which_domain_caught_it(rules):
    rule = rules.upsert("name", "Custom Load Power", "ecoflow_cloud", "de", "Andere Lasten", domain="sensor")

    assert rules.why(rule, "ecoflow_cloud", None, "sensor")["domain"] == "sensor"


def test_a_filter_for_another_domain_does_not_explain_this_entity(rules):
    rule = rules.upsert("name", "Custom Load Power", "ecoflow_cloud", "de", "Andere Lasten", domain="sensor")

    assert rules.matching_filter(rule, "ecoflow_cloud", None, "number") == {}
