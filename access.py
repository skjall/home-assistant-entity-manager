"""The gate in front of every route.

Home Assistant authenticates the user before it proxies a request through
Ingress, so an Ingress request is trusted. Everything that reaches the add-on
past Ingress does so over a published port, and who may be answered there is the
external_access setting (see external_access.py), which refuses by default.

Within what the setting allows, a generated token (see ApiTokenStore) opens a
named set of routes for scripts. How far that goes is the external_api setting:
reading the state, or changing it as well. The token is created on demand from
the web UI, shown once and stored only as a hash.
"""

import json
import logging
from typing import Any, Callable

from flask import abort, request

import external_access

logger = logging.getLogger(__name__)

# What a token opens over a published port. Matched against the route's rule
# rather than the request path, so a rule carrying a parameter is recognised
# too, and a route that is not named here stays unreachable from outside no
# matter what it does.
READ_ROUTES = frozenset(
    {
        "/api/rename_log",
        "/api/stats",
        "/api/areas",
        "/api/all_entities",
        "/api/hierarchy",
        "/api/naming/rules",
        "/api/naming/settings",
    }
)

# Only reachable with external_api set to write. These change the registry or
# the rules behind it; everything else a browser can do stays Ingress-only.
WRITE_ROUTES = frozenset(
    {
        "/api/naming/rules",
        "/api/naming/rules/<rule_id>",
        "/api/naming/settings",
        "/api/set_entity_override",
        "/api/rename_entity",
        "/api/execute",
    }
)


def route_allowed(rule: str, method: str, mode: str) -> bool:
    """True when ``mode`` opens this route for a caller outside Ingress."""
    if mode == external_access.API_OFF or not rule:
        return False
    if method in ("GET", "HEAD"):
        return rule in READ_ROUTES
    return mode == external_access.API_WRITE and rule in WRITE_ROUTES


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
        external_access setting has to allow that caller, the external_api
        setting has to open that route for that method, and a valid token has to
        be presented. The web UI is never among the opened routes: it is served
        through Ingress only.
        """
        if is_ingress_request():
            return None
        peer = peer_address()
        if not external_access.allows(peer):
            # Named, because a refusal that does not say who was refused is the
            # hardest kind to configure against - a proxy shows its own address,
            # not the browser's, and that is the address to allow.
            logger.warning(
                "Refused %s %s from %s: not in external_access (%s)",
                request.method,
                request.path,
                peer or "an unreadable address",
                ", ".join(external_access.describe()) or "empty",
            )
            abort(403)
        rule = request.url_rule.rule if request.url_rule else ""
        mode = external_access.api_mode()
        if not route_allowed(rule, request.method, mode):
            logger.warning("Refused %s %s from %s: external_api is %s", request.method, request.path, peer, mode)
            abort(403)
        store = token_store()
        if not store.exists() or not store.verify(provided_token()):
            logger.warning("Refused %s %s from %s: no valid token", request.method, request.path, peer)
            abort(401)
        return None


class Guard:
    """The gate in front of a mounted ASGI app, for what Flask never sees.

    An MCP client is not a browser inside Home Assistant: it arrives over the
    published port. So the same two conditions hold as for the API — the
    external_access setting has to allow that caller, and a valid token has to
    be presented — while a request that came through Ingress passes as it does
    everywhere else.
    """

    def __init__(self, app: Any, token_store: Callable[[], Any]) -> None:
        self.app = app
        self.token_store = token_store
        # Pass the wrapped app's startup and shutdown through, or it never runs.
        self.lifespan = getattr(app, "lifespan", None)

    @staticmethod
    def _token_from(headers: list) -> str:
        for name, value in headers:
            key = name.decode("latin-1").lower()
            if key == "authorization":
                text = value.decode("latin-1")
                if text.startswith("Bearer "):
                    return text[len("Bearer ") :].strip()
            elif key == "x-api-key":
                return value.decode("latin-1").strip()
        return ""

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        peer = (scope.get("client") or ("", 0))[0] or ""
        if not external_access.is_supervisor(peer):
            if not external_access.allows(peer):
                logger.warning(
                    "Refused MCP from %s: not in external_access (%s)",
                    peer or "an unreadable address",
                    ", ".join(external_access.describe()) or "empty",
                )
                await self._refuse(scope, send, 403, "external access is off")
                return
            store = self.token_store()
            if not store.exists() or not store.verify(self._token_from(scope.get("headers") or [])):
                logger.warning("Refused MCP from %s: no valid token", peer)
                await self._refuse(scope, send, 401, "a valid API token is required")
                return
        await self.app(scope, receive, send)

    @staticmethod
    async def _refuse(scope: dict, send: Callable, status: int, reason: str) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"error": reason}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})
