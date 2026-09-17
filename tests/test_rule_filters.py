"""How far a rule reaches: a list of filters instead of one fixed scope.

A rule used to carry exactly one scope, so saying "this wording, for these two
integrations" meant writing the rule twice and keeping both in step by hand.
Filters make that one rule. What has to hold: several filters are read as "or",
the narrowest one that matches decides, the same filter written twice is only
one, and two rules may never both answer to the same filter.
"""

import json

import pytest

from naming_rules import NamingRuleError, NamingRules


@pytest.fixture
def rules(tmp_path):
    return NamingRules(str(tmp_path / "naming_rules.json"), default_language="de")


def test_one_rule_can_name_several_integrations(rules):
    rules.upsert("name", "brightness", None, "de", "LED-Helligkeit")
    rule = rules.rules[0]

    rules.update(rule["id"], filters=[{"integration": "ecoflow_cloud"}, {"integration": "matter"}])

    assert rules.find("name", "Brightness", "ecoflow_cloud", "de")["id"] == rule["id"]
    assert rules.find("name", "Brightness", "matter", "de")["id"] == rule["id"]
    assert rules.find("name", "Brightness", "mqtt", "de") is None


def test_the_narrowest_filter_decides(rules):
    """Everywhere, one integration, one model - in that order of precedence."""
    rules.upsert("name", "brightness", None, "de", "Helligkeit")
    rules.upsert("name", "brightness", "ecoflow_cloud", "de", "LED-Helligkeit")
    rules.upsert("name", "brightness", "ecoflow_cloud", "de", "Anzeige", model="Smart Plug")

    assert rules.find("name", "Brightness", "mqtt", "de")["targets"]["de"] == "Helligkeit"
    assert rules.find("name", "Brightness", "ecoflow_cloud", "de")["targets"]["de"] == "LED-Helligkeit"
    narrow = rules.find("name", "Brightness", "ecoflow_cloud", "de", model="Smart Plug")
    assert narrow["targets"]["de"] == "Anzeige"


def test_the_same_filter_twice_is_once(rules):
    rules.upsert("name", "brightness", None, "de", "Helligkeit")
    rule = rules.rules[0]

    updated = rules.update(rule["id"], filters=[{"integration": "mqtt"}, {"integration": "mqtt"}])

    assert updated["filters"] == [{"integration": "mqtt"}]


def test_two_rules_may_not_claim_the_same_filter(rules):
    """Which one answered would otherwise depend on the order they were written in."""
    rules.upsert("name", "brightness", "mqtt", "de", "Helligkeit")
    rules.upsert("name", "brightness", None, "de", "Anzeige")
    wide = [rule for rule in rules.rules if not rule["filters"]][0]

    with pytest.raises(NamingRuleError, match="already covers"):
        rules.update(wide["id"], filters=[{"integration": "mqtt"}])


def test_a_rule_may_keep_its_own_filters(rules):
    """Rewriting a rule with the filters it already has is not a collision."""
    rules.upsert("name", "brightness", "mqtt", "de", "Helligkeit")
    rule = rules.rules[0]

    updated = rules.update(rule["id"], filters=[{"integration": "mqtt"}, {"integration": "matter"}])

    assert len(updated["filters"]) == 2


def test_a_filter_names_one_entity_or_a_kind_of_device(rules):
    rules.upsert("name", "brightness", None, "de", "Helligkeit")
    rule = rules.rules[0]

    with pytest.raises(NamingRuleError, match="not both"):
        rules.update(rule["id"], filters=[{"registry_id": "abc", "integration": "mqtt"}])


def test_an_empty_filter_is_refused(rules):
    """It would say "everywhere", which is what no filter already says."""
    rules.upsert("name", "brightness", None, "de", "Helligkeit")
    rule = rules.rules[0]

    with pytest.raises(NamingRuleError):
        rules.update(rule["id"], filters=[{}])


def test_filters_come_back_narrowest_first(rules):
    rules.upsert("name", "brightness", None, "de", "Helligkeit")
    rule = rules.rules[0]

    updated = rules.update(
        rule["id"],
        filters=[{"integration": "mqtt"}, {"integration": "matter", "model": "Plug"}, {"registry_id": "r1"}],
    )

    assert [sorted(one) for one in updated["filters"]] == [["registry_id"], ["integration", "model"], ["integration"]]


def test_a_rule_written_before_filters_keeps_its_reach(tmp_path):
    """The one scope it carried becomes its one filter, on load."""
    path = tmp_path / "naming_rules.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "language": "de",
                "rules": [
                    {
                        "id": "r_old",
                        "match": {"kind": "name", "value": "brightness", "integration": "mqtt", "model": None},
                        "targets": {"de": "Helligkeit"},
                        "source": "migrated",
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    rules = NamingRules(str(path), default_language="de")

    assert rules.rules[0]["filters"] == [{"integration": "mqtt"}]
    assert "integration" not in rules.rules[0]["match"]
    assert rules.find("name", "Brightness", "mqtt", "de")["id"] == "r_old"
    assert rules.find("name", "Brightness", "zha", "de") is None


def test_a_rule_that_reached_everywhere_still_does(tmp_path):
    path = tmp_path / "naming_rules.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "language": "de",
                "rules": [
                    {
                        "id": "r_wide",
                        "match": {"kind": "name", "value": "brightness", "integration": None, "model": None},
                        "targets": {"de": "Helligkeit"},
                        "source": "migrated",
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    rules = NamingRules(str(path), default_language="de")

    assert rules.rules[0]["filters"] == []
    assert rules.find("name", "Brightness", "anything", "de")["id"] == "r_wide"


def test_what_matched_says_which_filter_caught_it(rules):
    rules.upsert("name", "brightness", None, "de", "Helligkeit")
    rule = rules.rules[0]
    rules.update(rule["id"], filters=[{"integration": "ecoflow_cloud"}, {"integration": "matter"}])

    why = rules.why(rule, "matter", None)

    assert why["integration"] == "matter"
    assert why["value"] == "brightness"
    assert len(why["filters"]) == 2


def test_a_rule_over_several_integrations_has_no_single_one(rules):
    """It cannot be measured against one integration's built-in wording."""
    rules.upsert("name", "brightness", None, "de", "Helligkeit")
    rule = rules.rules[0]

    rules.update(rule["id"], filters=[{"integration": "ecoflow_cloud"}, {"integration": "matter"}])
    assert rules.sole_integration(rule) is None

    rules.update(rule["id"], filters=[{"integration": "matter"}])
    assert rules.sole_integration(rule) == "matter"


def test_the_whole_list_of_places_can_be_replaced_at_once(rules):
    """One place at a time is add_filter; this is the only other honest change.

    There is deliberately no way to set a single scope any more: on a rule
    reaching three places that could only mean throwing two away.
    """
    rules.upsert("name", "brightness", None, "de", "Helligkeit")
    rule = rules.rules[0]

    updated = rules.update(rule["id"], filters=[{"integration": "mqtt"}, {"integration": "matter"}])

    assert updated["filters"] == [{"integration": "matter"}, {"integration": "mqtt"}]
