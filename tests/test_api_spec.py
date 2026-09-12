"""The API description has to describe this app, not a past version of it.

Everything external hangs off api_spec: the documentation a reader browses, the
routes the gate opens, the tools an assistant gets. A description that has
drifted away from the app is therefore not a documentation problem but a broken
promise in three places at once.
"""

import json

import api_spec
import web_ui

# Routes that are deliberately not part of the API. Each is named here so that
# leaving one out stays a decision somebody made rather than an oversight.
INTERNAL_ONLY = {
    "/api/api_token",  # the key to the API cannot be fetched through the API
    "/api/openapi.json",  # the description itself, served to the settings page
    "/api/docs",  # the page that renders it
    "/api/preview",  # preview and execute pair up through an id that only
    "/api/execute",  # means something inside one browser session
    "/api/update_mapping",  # works on such a preview
    "/api/languages",  # which translations the interface ships
    "/api/naming_templates/sample",  # fills a field in the template editor
    "/api/naming_templates/sample/entities",
}


def real_routes():
    found = {}
    for rule in web_ui.app.url_map.iter_rules():
        if rule.rule.startswith("/api/"):
            found.setdefault(rule.rule, set()).update(m for m in rule.methods if m not in ("HEAD", "OPTIONS"))
    return found


def test_every_described_operation_exists():
    real = real_routes()

    for operation in api_spec.OPERATIONS:
        assert operation.path in real, f"{operation.name} describes a route that does not exist"
        assert operation.method.upper() in real[operation.path], f"{operation.name} uses a method the route refuses"


def test_every_route_is_described_or_deliberately_not():
    described = {operation.path for operation in api_spec.OPERATIONS}
    forgotten = sorted(set(real_routes()) - described - INTERNAL_ONLY)

    assert forgotten == [], f"routes neither described nor listed as internal: {forgotten}"


def test_the_gate_opens_exactly_what_is_described():
    import access

    assert access.READ_ROUTES == {o.path for o in api_spec.OPERATIONS if o.method == "get"}
    assert access.WRITE_ROUTES == {o.path for o in api_spec.OPERATIONS if o.method != "get"}


def test_each_operation_is_named_once():
    names = [operation.name for operation in api_spec.OPERATIONS]

    assert len(names) == len(set(names)), "two operations share a name, so one tool would win"


def test_the_document_is_valid_enough_to_be_read():
    document = api_spec.document()

    assert document["openapi"].startswith("3.")
    assert document["info"]["title"]
    assert document["paths"]
    for path, operations in document["paths"].items():
        assert path.startswith("/api/")
        assert "<" not in path, "an address still in Flask's spelling"
        for method, entry in operations.items():
            assert entry["operationId"]
            assert entry["summary"]
            assert entry["description"]
            assert entry["responses"]["200"]
            for parameter in entry.get("parameters", []):
                assert parameter["description"], f"{entry['operationId']}.{parameter['name']}"
    json.dumps(document)  # anything unserialisable would break the endpoint


def test_path_parameters_are_all_declared():
    """A path parameter nobody declared becomes a tool nobody can call."""
    document = api_spec.document()

    for operation in api_spec.OPERATIONS:
        entry = document["paths"][operation.openapi_path()][operation.method]
        declared = {p["name"] for p in entry.get("parameters", []) if p["in"] == "path"}
        assert declared == set(operation.path_parameters()), operation.name


def test_a_body_field_is_always_explained():
    for operation in api_spec.OPERATIONS:
        for field, schema in operation.body.items():
            assert schema.get("description"), f"{operation.name}.{field}"


def test_the_document_is_served_under_the_path_it_was_asked_for():
    """Through Ingress the add-on lives under a prefix; a document that does
    not carry it sends every call to Home Assistant itself."""
    client = web_ui.app.test_client()
    ingress = {"REMOTE_ADDR": "172.30.32.2"}
    prefix = "/api/hassio_ingress/abc123"

    plain = client.get("/api/openapi.json", environ_overrides=ingress).json
    proxied = client.get(
        "/api/openapi.json",
        environ_overrides=ingress,
        headers={"X-Ingress-Path": prefix},
    ).json

    assert plain["servers"] == [{"url": "/"}]
    assert proxied["servers"] == [{"url": prefix + "/"}]
