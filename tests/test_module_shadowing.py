"""A module must not define away something it imported.

Python keeps one name per module, so a route handler called like the helper it
was meant to call silently takes its place - for the whole module, including
the calls that came before the definition. That happened: `/api/sync_z2m_name`
was served by a view named `sync_z2m_name`, and every call to the Zigbee2MQTT
helper of that name landed on the view instead, which takes no arguments.

Nothing in the language objects and no test that exercises one function alone
would notice, so the shape is checked here across every module at once.
"""

import ast
import pathlib

import routes_entities
import z2m

REPO = pathlib.Path(__file__).resolve().parent.parent


def shadowed_names(source: str):
    """Top-level definitions that reuse the name of a top-level import."""
    tree = ast.parse(source)
    imported = {}
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imported[alias.asname or alias.name.split(".")[0]] = node.lineno
    return [
        (node.name, node.lineno, imported[node.name])
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in imported
    ]


def test_no_module_defines_over_its_own_imports():
    offenders = []
    for path in sorted(REPO.glob("*.py")):
        for name, defined_at, imported_at in shadowed_names(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.name}:{defined_at} def {name} hides the import from line {imported_at}")
    assert offenders == []


def test_the_zigbee_name_helper_is_still_the_helper():
    """The concrete case above, named so a regression says what broke."""
    assert routes_entities.sync_z2m_name is z2m.sync_z2m_name
