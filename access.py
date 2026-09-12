"""The gate in front of every route.

Home Assistant authenticates the user before it proxies a request through
Ingress, so an Ingress request is trusted. Everything that reaches the add-on
past Ingress does so over a published port, and who may be answered there is the
external_access setting (see external_access.py), which refuses by default.

Within what the setting allows, a generated token (see ApiTokenStore) opens one
read-only route for scripts: GET /api/rename_log. The token is created on demand
from the web UI, shown once and stored only as a hash.
"""

from typing import Any, Callable

from flask import abort, request

import external_access

# Paths a token opens over a directly-exposed port. Read-only.
EXTERNAL_PATHS = frozenset({"/api/rename_log"})


class CapturePeerIP:
    """WSGI middleware recording the real TCP peer address.

    Installed as the outermost layer so it sees the untouched ``REMOTE_ADDR``
    before ProxyFix rewrites it from forwarded headers. This lets the gate tell
    genuine Ingress traffic apart from direct port access, which a client cannot
    forge via request headers.
    """

    def __init__(self, wsgi_app: object) -> None:
        self.wsgi_app = wsgi_app

    def __call__(self, environ: dict, start_response: object) -> object:
        environ["entity_manager.peer_addr"] = environ.get("REMOTE_ADDR", "")
        return self.wsgi_app(environ, start_response)


def peer_address() -> str:
    """The request's real TCP peer, captured before ProxyFix.

    Reading it from the environment rather than from a forwarded header means a
    client cannot claim to be Ingress by setting one on a direct connection.
    """
    return request.environ.get("entity_manager.peer_addr", "")


def is_ingress_request() -> bool:
    """Return True when the request came through the Supervisor's Ingress."""
    return external_access.is_supervisor(peer_address())


def provided_token() -> str:
    """Extract the bearer token from Authorization (or the X-API-Key header)."""
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer ") :].strip()
    return request.headers.get("X-API-Key", "").strip()


def install(app: Any, token_store: Callable[[], Any]) -> None:
    """Put the gate in front of ``app``; ``token_store`` is read per request."""
    app.wsgi_app = CapturePeerIP(app.wsgi_app)

    @app.before_request
    def _enforce_access() -> None:
        """Refuse what should not be reachable past Home Assistant's Ingress.

        An Ingress request passes untouched. A direct request over a published
        port has no authentication behind it, so three things must hold: the
        external_access setting has to allow that caller, the route has to be
        one of the few meant for callers outside, and a valid token has to be
        presented. The web UI is not among them: it is served through Ingress
        only.
        """
        if is_ingress_request():
            return None
        if not external_access.allows(peer_address()):
            abort(403)
        if request.path not in EXTERNAL_PATHS or request.method != "GET":
            abort(403)
        store = token_store()
        if not store.exists() or not store.verify(provided_token()):
            abort(401)
        return None
