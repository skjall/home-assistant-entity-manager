"""Tests for the /api/* access gate.

Ingress traffic (real peer in the Supervisor network) is trusted; direct
(non-Ingress) access is limited to GET /api/rename_log with a valid generated
token. The real peer is simulated via REMOTE_ADDR, which the _CapturePeerIP
middleware copies into the per-request peer address used by the gate.
"""

import pytest

import web_ui

INGRESS_IP = "172.30.32.2"  # inside the Supervisor network
DIRECT_IP = "192.168.1.50"  # a LAN/host address (not Ingress)


@pytest.fixture
def store():
    """Reset the shared token store before and after each test."""
    s = web_ui.renamer_state["api_token_store"]
    s.revoke()
    yield s
    s.revoke()


@pytest.fixture
def client():
    web_ui.app.config["TESTING"] = True
    return web_ui.app.test_client()


def _req(client, path, ip, token=None, method="GET"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.open(path, method=method, headers=headers, environ_overrides={"REMOTE_ADDR": ip})


# --------------------------------------------------------------------------- #
# Gate inactive when no token exists (default behaviour preserved)
# --------------------------------------------------------------------------- #


def test_no_token_allows_direct_lookup(client, store):
    resp = _req(client, "/api/rename_log?entity_id=light.x", DIRECT_IP)
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Token exists: Ingress is trusted
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
# Token exists: direct access is restricted to the read-only lookup
# --------------------------------------------------------------------------- #


def test_direct_lookup_with_valid_token_allowed(client, store):
    token = store.generate()
    resp = _req(client, "/api/rename_log?entity_id=light.x", DIRECT_IP, token=token)
    assert resp.status_code == 200


def test_direct_lookup_with_wrong_token_rejected(client, store):
    store.generate()
    resp = _req(client, "/api/rename_log?entity_id=light.x", DIRECT_IP, token="em_wrong")
    assert resp.status_code == 401


def test_direct_lookup_without_token_rejected(client, store):
    store.generate()
    resp = _req(client, "/api/rename_log?entity_id=light.x", DIRECT_IP)
    assert resp.status_code == 401


def test_direct_wrong_method_on_lookup_forbidden(client, store):
    token = store.generate()
    resp = _req(client, "/api/rename_log", DIRECT_IP, token=token, method="POST")
    assert resp.status_code == 403


def test_direct_write_path_forbidden_even_with_token(client, store):
    token = store.generate()
    resp = _req(client, "/api/rename_entity", DIRECT_IP, token=token, method="POST")
    assert resp.status_code == 403


def test_direct_token_management_forbidden_even_with_token(client, store):
    # Generating/revoking tokens must stay Ingress-only.
    token = store.generate()
    resp = _req(client, "/api/api_token", DIRECT_IP, token=token, method="POST")
    assert resp.status_code == 403


def test_non_api_path_not_gated(client, store):
    store.generate()
    resp = _req(client, "/", DIRECT_IP)
    assert resp.status_code != 403
