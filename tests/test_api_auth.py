"""Tests for the access gate in front of every route.

Home Assistant authenticates the user before it proxies a request through
Ingress, so Ingress traffic is trusted. A direct request over a published port
is answered only where the external_access setting allows that caller, only on
the routes external_api opens, and only with a valid token. Everything else -
the web interface above all - stays Ingress-only either way.

The real peer is simulated via REMOTE_ADDR, which the _CapturePeerIP middleware
copies into the per-request peer address the gate reads.
"""

import pytest

import web_ui

INGRESS_IP = "172.30.32.2"  # inside the Supervisor network
DIRECT_IP = "192.168.1.50"  # a LAN/host address (not Ingress)
INTERNET_IP = "203.0.113.7"  # routable from outside


@pytest.fixture
def store():
    """Reset the shared token store before and after each test."""
    s = web_ui.renamer_state["api_token_store"]
    s.revoke()
    yield s
    s.revoke()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("EXTERNAL_ACCESS", raising=False)
    monkeypatch.delenv("EXTERNAL_API", raising=False)
    web_ui.app.config["TESTING"] = True
    return web_ui.app.test_client()


def _req(client, path, ip, token=None, method="GET"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.open(path, method=method, headers=headers, environ_overrides={"REMOTE_ADDR": ip})


# --------------------------------------------------------------------------- #
# The setting decides whether a direct caller is answered at all
# --------------------------------------------------------------------------- #


def test_by_default_a_direct_caller_gets_nothing(client, store):
    assert _req(client, "/api/rename_log?entity_id=light.x", DIRECT_IP).status_code == 403
    assert _req(client, "/", DIRECT_IP).status_code == 403


def test_a_token_does_not_open_the_port_by_itself(client, store):
    token = store.generate()

    resp = _req(client, "/api/rename_log?entity_id=light.x", DIRECT_IP, token=token)

    assert resp.status_code == 403


def test_the_local_setting_answers_the_home_network_only(client, store, monkeypatch):
    monkeypatch.setenv("EXTERNAL_ACCESS", "lan")
    token = store.generate()

    assert _req(client, "/api/rename_log?entity_id=light.x", DIRECT_IP, token=token).status_code == 200
    assert _req(client, "/api/rename_log?entity_id=light.x", INTERNET_IP, token=token).status_code == 403


def test_the_open_setting_answers_everyone(client, store, monkeypatch):
    monkeypatch.setenv("EXTERNAL_ACCESS", "any")
    token = store.generate()

    resp = _req(client, "/api/rename_log?entity_id=light.x", INTERNET_IP, token=token)

    assert resp.status_code == 200


def test_the_web_ui_is_never_served_over_the_port(client, store, monkeypatch):
    """The port is for scripts holding a token, not for the interface."""
    monkeypatch.setenv("EXTERNAL_ACCESS", "any")
    token = store.generate()

    assert _req(client, "/", DIRECT_IP, token=token).status_code == 403
    assert _req(client, "/settings/naming", DIRECT_IP, token=token).status_code == 403


# --------------------------------------------------------------------------- #
# Ingress is trusted
# --------------------------------------------------------------------------- #


def test_ingress_lookup_without_token_allowed(client, store):
    store.generate()
    resp = _req(client, "/api/rename_log?entity_id=light.x", INGRESS_IP)
    assert resp.status_code == 200


def test_ingress_write_path_not_blocked_by_gate(client, store):
    store.generate()
    resp = _req(client, "/api/rename_entity", INGRESS_IP, method="POST")
    assert resp.status_code != 403


def test_ingress_token_management_allowed(client, store):
    store.generate()
    resp = _req(client, "/api/api_token", INGRESS_IP)
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# What the setting lets in still has to hold a token for the API
# --------------------------------------------------------------------------- #


def test_direct_lookup_with_valid_token_allowed(client, store, monkeypatch):
    monkeypatch.setenv("EXTERNAL_ACCESS", "lan")
    token = store.generate()

    resp = _req(client, "/api/rename_log?entity_id=light.x", DIRECT_IP, token=token)

    assert resp.status_code == 200


def test_direct_lookup_with_wrong_token_rejected(client, store, monkeypatch):
    monkeypatch.setenv("EXTERNAL_ACCESS", "lan")
    store.generate()

    resp = _req(client, "/api/rename_log?entity_id=light.x", DIRECT_IP, token="em_wrong")

    assert resp.status_code == 401


def test_direct_lookup_without_token_rejected(client, store, monkeypatch):
    monkeypatch.setenv("EXTERNAL_ACCESS", "lan")
    store.generate()

    resp = _req(client, "/api/rename_log?entity_id=light.x", DIRECT_IP)

    assert resp.status_code == 401


def test_direct_wrong_method_on_lookup_forbidden(client, store, monkeypatch):
    monkeypatch.setenv("EXTERNAL_ACCESS", "lan")
    token = store.generate()

    resp = _req(client, "/api/rename_log", DIRECT_IP, token=token, method="POST")

    assert resp.status_code == 403


def test_direct_write_path_forbidden_even_with_token(client, store, monkeypatch):
    monkeypatch.setenv("EXTERNAL_ACCESS", "lan")
    token = store.generate()

    resp = _req(client, "/api/rename_entity", DIRECT_IP, token=token, method="POST")

    assert resp.status_code == 403


def test_direct_token_management_forbidden_even_with_token(client, store, monkeypatch):
    """Generating and revoking tokens stays Ingress-only."""
    monkeypatch.setenv("EXTERNAL_ACCESS", "lan")
    token = store.generate()

    resp = _req(client, "/api/api_token", DIRECT_IP, token=token, method="POST")

    assert resp.status_code == 403


def test_a_forwarded_header_cannot_claim_to_be_ingress(client, store):
    resp = _req(
        client,
        "/api/rename_log?entity_id=light.x",
        INTERNET_IP,
    )
    assert resp.status_code == 403

    resp = client.get(
        "/api/rename_log?entity_id=light.x",
        headers={"X-Forwarded-For": INGRESS_IP, "X-Ingress-Path": "/api/hassio_ingress/x"},
        environ_overrides={"REMOTE_ADDR": INTERNET_IP},
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# How far the token reaches is its own setting
# --------------------------------------------------------------------------- #


def test_reading_covers_more_than_the_rename_log(client, store, monkeypatch):
    """A script that may read should not have to go through one keyhole."""
    monkeypatch.setenv("EXTERNAL_ACCESS", "lan")
    token = store.generate()

    assert _req(client, "/api/naming/rules", DIRECT_IP, token=token).status_code == 200
    assert _req(client, "/api/naming/settings", DIRECT_IP, token=token).status_code == 200


def test_reading_does_not_include_changing(client, store, monkeypatch):
    monkeypatch.setenv("EXTERNAL_ACCESS", "lan")
    monkeypatch.setenv("EXTERNAL_API", "read")
    token = store.generate()

    assert _req(client, "/api/naming/rules", DIRECT_IP, token=token, method="POST").status_code == 403
    assert _req(client, "/api/rename_entity", DIRECT_IP, token=token, method="POST").status_code == 403


def test_writing_opens_the_routes_that_change_something(client, store, monkeypatch):
    monkeypatch.setenv("EXTERNAL_ACCESS", "lan")
    monkeypatch.setenv("EXTERNAL_API", "write")
    token = store.generate()

    # Reaching the route is what the gate decides; what the route then makes of
    # an empty body is the route's business.
    assert _req(client, "/api/naming/rules", DIRECT_IP, token=token, method="POST").status_code != 403
    assert _req(client, "/api/rename_entity", DIRECT_IP, token=token, method="POST").status_code != 403


def test_writing_still_leaves_everything_unnamed_closed(client, store, monkeypatch):
    """Write means the named routes, not every route that takes a POST."""
    monkeypatch.setenv("EXTERNAL_ACCESS", "lan")
    monkeypatch.setenv("EXTERNAL_API", "write")
    token = store.generate()

    assert _req(client, "/api/api_token", DIRECT_IP, token=token, method="POST").status_code == 403
    assert _req(client, "/api/update_mapping", DIRECT_IP, token=token, method="POST").status_code == 403
    assert _req(client, "/", DIRECT_IP, token=token).status_code == 403


def test_the_api_can_be_switched_off_while_the_network_stays_allowed(client, store, monkeypatch):
    """Turning the API off must close it without touching who may connect."""
    monkeypatch.setenv("EXTERNAL_ACCESS", "lan")
    monkeypatch.setenv("EXTERNAL_API", "off")
    token = store.generate()

    assert _req(client, "/api/rename_log?entity_id=light.x", DIRECT_IP, token=token).status_code == 403
    assert _req(client, "/api/naming/rules", DIRECT_IP, token=token).status_code == 403


def test_a_named_network_decides_who_is_answered(client, store, monkeypatch):
    monkeypatch.setenv("EXTERNAL_ACCESS", "192.168.1.0/24")
    token = store.generate()

    assert _req(client, "/api/naming/rules", DIRECT_IP, token=token).status_code == 200
    assert _req(client, "/api/naming/rules", "192.168.2.50", token=token).status_code == 403
