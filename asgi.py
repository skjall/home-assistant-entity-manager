"""The application as an ASGI app, so more than plain request-response fits.

Flask speaks WSGI: a request goes in, an answer comes back, and the connection
is done. That is enough for the web interface but not for a connection that
stays open and receives more as work progresses, which is how an MCP client
talks to a server. ASGI is the newer arrangement that allows it, so the Flask
application is wrapped and served by an ASGI server; anything that needs the
long-lived kind is mounted beside it.

Wrapping means Flask's own views run in a worker thread, several at a time. The
stores they write are guarded (see json_store.py) and a registry load lets only
one caller in at a time (see EntityRestructurer.load_structure), so that is
safe — it was not before those two.

The bridge is a2wsgi rather than asgiref: asgiref sends every WSGI call through
a single thread, which measured as no concurrency at all — eight requests that
each wait 0.2 s took 1.61 s instead of 0.20 s, and one slow request held up
every other. a2wsgi uses a real pool of worker threads.
"""

import logging
import os
from typing import Any, Optional

from a2wsgi import WSGIMiddleware

logger = logging.getLogger(__name__)


def build(flask_app: Any, mcp_app: Optional[Any] = None) -> Any:
    """Return the ASGI application: the web interface, plus MCP when present."""
    # Four measured best for this mix of waiting and computing: fewer leaves a
    # caller queued behind one slow request, more only adds thread switching.
    wsgi = WSGIMiddleware(flask_app, workers=int(os.getenv("WEB_UI_THREADS", 4)))
    if mcp_app is None:
        return wsgi

    from starlette.applications import Starlette
    from starlette.routing import Mount

    # The MCP app keeps state for the length of a session, which it sets up and
    # tears down through the lifespan; mounting alone would never run that, so
    # the outer application borrows it.
    return Starlette(
        routes=[Mount("/mcp", app=mcp_app), Mount("/", app=_BarePath("/mcp", mcp_app, wsgi))],
        lifespan=getattr(mcp_app, "lifespan", None),
    )


class _BarePath:
    """Serve ``/mcp`` as what every client means by it: ``/mcp/``.

    A mount only matches the addresses below it, so the bare mount point falls
    through to whatever comes next — here the web interface, which answers a
    stranger with 403. Clients write the address without a trailing slash, so
    that is the one that has to work.
    """

    def __init__(self, mount: str, mounted: Any, otherwise: Any) -> None:
        self.mount = mount
        self.mounted = mounted
        self.otherwise = otherwise

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") in ("http", "websocket") and scope.get("path") == self.mount:
            # Hand the mounted app exactly what a mount would have handed it
            # for the slashed address: the full path, and the mount point as
            # the root the app is served under.
            inner = dict(scope)
            inner["path"] = self.mount + "/"
            inner["raw_path"] = inner["path"].encode()
            inner["root_path"] = scope.get("root_path", "") + self.mount
            await self.mounted(inner, receive, send)
            return
        await self.otherwise(scope, receive, send)


def serve(application: Any, port: int) -> None:
    """Run the application on an ASGI server."""
    import uvicorn

    workers = int(os.getenv("WEB_UI_THREADS", 4))
    logger.info("Starting Web UI (uvicorn) on port %d", port)
    uvicorn.run(
        application,
        host="0.0.0.0",
        port=port,
        # Home Assistant's own log already carries every line the add-on prints.
        log_config=None,
        access_log=False,
        timeout_keep_alive=65,
        limit_concurrency=workers * 8,
    )
