"""Two callers writing the same store at the same time must not lose an edit.

Every store reads its whole file, changes something and writes it back. Under a
server that answers requests in parallel, an unguarded store would let the
second writer save the state the first one had already replaced.
"""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from naming_overrides import NamingOverrides
from naming_rules import NamingRules
from naming_templates import NamingTemplates

WRITERS = 8
PER_WRITER = 12


@pytest.fixture
def rules(tmp_path):
    return NamingRules(str(tmp_path / "naming_rules.json"), default_language="de")


def test_rules_written_at_once_all_survive(rules):
    def write(worker):
        for number in range(PER_WRITER):
            rules.upsert("name", f"w{worker}-{number}", None, "de", f"Wert {worker}-{number}")

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        list(pool.map(write, range(WRITERS)))

    on_disk = json.loads(rules.storage_path.read_text(encoding="utf-8"))
    assert len(on_disk["rules"]) == WRITERS * PER_WRITER
    assert len(rules.rules) == WRITERS * PER_WRITER


def test_deleting_while_others_write_leaves_the_file_readable(rules):
    for number in range(WRITERS * PER_WRITER):
        rules.upsert("name", f"start-{number}", None, "de", f"Wert {number}")
    doomed = [rule["id"] for rule in rules.rules][: WRITERS * PER_WRITER // 2]

    def delete(rule_id):
        rules.delete(rule_id)

    def add(number):
        rules.upsert("name", f"added-{number}", None, "de", f"Neu {number}")

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        futures = [pool.submit(delete, rule_id) for rule_id in doomed]
        futures += [pool.submit(add, number) for number in range(WRITERS * PER_WRITER // 2)]
        for future in futures:
            future.result()

    on_disk = json.loads(rules.storage_path.read_text(encoding="utf-8"))
    assert len(on_disk["rules"]) == len(rules.rules)


def test_overrides_written_at_once_all_survive(tmp_path):
    overrides = NamingOverrides(str(tmp_path / "naming_overrides.json"))

    def write(worker):
        for number in range(PER_WRITER):
            overrides.set_entity_override(f"reg-{worker}-{number}", f"Name {worker}-{number}")

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        list(pool.map(write, range(WRITERS)))

    on_disk = json.loads(open(overrides.storage_path, encoding="utf-8").read())
    assert len(on_disk["entities"]) == WRITERS * PER_WRITER


def test_templates_written_at_once_leave_a_readable_file(tmp_path):
    templates = NamingTemplates(str(tmp_path / "naming_templates.json"))
    wanted = [
        {"device_name": "{device}", "entity_name": "{device} {entity}", "entity_id": "{device} {entity}"},
        {
            "device_name": "{area} {device}",
            "entity_name": "{area} {device} {entity}",
            "entity_id": "{area} {device} {entity}",
        },
    ]

    def write(number):
        templates.set_templates(wanted[number % len(wanted)])

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        list(pool.map(write, range(WRITERS * 2)))

    on_disk = json.loads(templates.storage_path.read_text(encoding="utf-8"))
    assert on_disk["templates"] in wanted
