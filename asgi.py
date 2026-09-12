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
"""

import logging
import os
from typing import Any, Optional

from asgiref.wsgi import WsgiToAsgi

logger = logging.getLogger(__name__)


def build(flask_app: Any, mcp_app: Optional[Any] = None) -> Any:
    """Return the ASGI application: the web interface, plus MCP when present."""
    wsgi = WsgiToAsgi(flask_app)
    if mcp_app is None:
        return wsgi

    from starlette.applications import Starlette
    from starlette.routing import Mount

    # The MCP app keeps state for the length of a session, which it sets up and
    # tears down through the lifespan; mounting alone would never run that, so
    # the outer application borrows it.
    return Starlette(
        routes=[Mount("/mcp", app=mcp_app), Mount("/", app=wsgi)],
        lifespan=getattr(mcp_app, "lifespan", None),
    )


def serve(application: Any, port: int) -> None:
    """Run the application on an ASGI server."""
    import uvicorn

    workers = int(os.getenv("WEB_UI_THREADS", 8))
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
