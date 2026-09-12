import os
import sys
import tempfile

# Make the add-on modules (in the repo root) importable from tests/.
sys.path.insert(0, os.path.dirname(__file__))

# Point persistent storage at a writable temp dir for tests/CI, where the
# add-on's default /data mount does not exist. Importing web_ui creates
# DATA_DIR-backed stores at import time, so this must run before collection.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="ha-em-test-"))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _requests_arrive_through_ingress(monkeypatch):
    """Let a test client look like Home Assistant's Ingress proxy.

    In a running add-on every request from the web UI arrives that way, and the
    add-on answers a direct caller only where its external_access setting says
    so. Tests about that setting build their own client and set the peer
    address themselves.
    """
    import web_ui

    original = web_ui.app.test_client

    def test_client(*args, **kwargs):
        client = original(*args, **kwargs)
        client.environ_base["REMOTE_ADDR"] = "172.30.32.2"
        return client

    monkeypatch.setattr(web_ui.app, "test_client", test_client)
