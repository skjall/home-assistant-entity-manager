"""The add-on's own API, offered to an assistant as tools.

Nothing here describes what the tools do: that is written down once in
api_spec.py and turned into tools from there. A capability the API has is
therefore a capability an assistant has, without anyone having to add it twice,
and the wording an assistant reads is the wording a person reads in the API
documentation.

The tools call the app in-process rather than over the network, so no request
leaves the container and the published port is not used to talk to ourselves.
Those internal calls arrive with a Supervisor address, which the access gate
trusts — the real caller has already been through that gate on the way in (see
access.Guard), and checking again here would only check the wrong address.
"""

import logging
import os
from typing import Any, Dict, Optional

import api_spec

logger = logging.getLogger(__name__)

# Nothing is offered; the endpoint is not even mounted.
OFF = "off"
# Only the calls that answer questions.
READ = "read"
# Everything, including the calls that change the user's home.
WRITE = "write"

MODES = (OFF, READ, WRITE)

# The address the in-process calls appear to come from. Inside the Supervisor's
# range, which the gate reads as "already authenticated by Home Assistant".
INTERNAL_PEER = ("172.30.32.1", 0)

# Not a real host: the transport never opens a socket, but a base is required.
INTERNAL_BASE = "http://entity-manager"

INSTRUCTIONS = """
This is a Home Assistant installation's entity manager. It decides what every
entity is called and can write those names back into Home Assistant.

How a name comes about: a rule gives one supplied type — a translation key, a
device class, or a plain supplied name — a name of the user's choosing, and a
template assembles the final name out of area, device and type. An exception
overrides both for a single entity. Prefer a rule where the same correction
would apply to more than one entity; an exception is for the one that is
genuinely different.

Before changing anything, look: naming_for says what an entity would be called
and which source decided it, and dependencies says what refers to it. Renaming
rewrites the automations, scripts, scenes and dashboards that named the old id.
Say what you are about to change before you change it.
""".strip()


def mode() -> str:
    """What the assistant interface offers, from the add-on's mcp option."""
    configured = (os.getenv("MCP") or "").strip().lower()
    if configured in MODES:
        return configured
    if configured:
        logger.warning("Unknown mcp value %r, switching the assistant interface off", configured)
    return OFF


def spec_for(chosen: str) -> Dict[str, Any]:
    """The API description, narrowed to what this mode offers."""
    document = api_spec.document(base_url=INTERNAL_BASE)
    if chosen == WRITE:
        return document
    narrowed = {}
    for path, operations in document["paths"].items():
        reading = {method: entry for method, entry in operations.items() if method == "get"}
        if reading:
            narrowed[path] = reading
    document["paths"] = narrowed
    return document


def _in_process_client(flask_app: Any) -> Any:
    """A client that reaches the app without touching the network."""
    from a2wsgi import WSGIMiddleware
    import httpx2

    transport = httpx2.ASGITransport(app=WSGIMiddleware(flask_app), client=INTERNAL_PEER)
    return httpx2.AsyncClient(transport=transport, base_url=INTERNAL_BASE, timeout=120.0)


def build(flask_app: Any, configured: Optional[str] = None) -> Optional[Any]:
    """The server for the configured mode, or None when it is switched off."""
    chosen = configured or mode()
    if chosen == OFF:
        return None

    from fastmcp import FastMCP

    document = spec_for(chosen)
    server = FastMCP.from_openapi(
        document,
        client=_in_process_client(flask_app),
        name="Home Assistant Entity Manager",
        instructions=INSTRUCTIONS,
        # The routes answer richer shapes than the description pins down;
        # validating every answer would refuse working calls over a gap in the
        # documentation rather than over a real fault.
        validate_output=False,
    )
    offered = sum(len(operations) for operations in document["paths"].values())
    logger.info("MCP server ready (%s), %d tools", chosen, offered)
    return server
