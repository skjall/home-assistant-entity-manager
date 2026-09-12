"""Routes about the add-on itself: its token, its reach, its log and its API description.

The token management and the documentation pages are for the settings page and
stay behind Ingress; the rename log and the reachability are part of the API a
token opens.
"""

import json
import logging
import os
import urllib.request

from flask import Blueprint, jsonify, render_template, request

import api_spec
from app_state import renamer_state
import external_access
import mcp_server

logger = logging.getLogger(__name__)

system = Blueprint("system", __name__)


@system.route("/api/rename_log", methods=["GET"])
def rename_log_lookup():
    """Resolve an entity_id against the rename audit log.

    Query parameter ``entity_id`` (the old / vanished id). Follows the rename
    chain forward and returns the current id plus the hop history, e.g.::

        GET /api/rename_log?entity_id=light.kitchen_old

        {
          "query": "light.kitchen_old",
          "found": true,
          "renamed": true,
          "current_entity_id": "light.kitchen_ceiling",
          "history": [ {"timestamp": ..., "old_entity_id": ...,
                        "new_entity_id": ..., "friendly_name": ...} ]
        }

    ``found`` is ``false`` when the id was never renamed (or is unknown).
    """
    entity_id = request.args.get("entity_id", "").strip()
    if not entity_id:
        return jsonify({"error": "Missing required query parameter: entity_id"}), 400

    rename_log = renamer_state["rename_log"]
    return jsonify(rename_log.search(entity_id))


def _published_ports() -> dict:
    """Ask the Supervisor which of this add-on's ports are published on the host.

    A container cannot see its own port mapping; the Supervisor knows it. When
    the question cannot be answered the page simply says nothing about the port.
    """
    token = os.getenv("SUPERVISOR_TOKEN")
    if not token:
        return {}
    ask = urllib.request.Request("http://supervisor/addons/self/info", headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(ask, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as error:
        logger.warning("Could not ask the Supervisor about the network: %s", error)
        return {}
    network = (data.get("data") or {}).get("network") or {}
    return {port: host for port, host in network.items() if host}


@system.route("/api/network", methods=["GET"])
def network_status():
    """How the add-on can be reached: the setting and what is actually published.

    Both halves matter and they live in different menus: publishing a port is
    done in the add-on's network settings, and who may be answered there is this
    add-on's own setting. Seeing them together is what makes a mistake visible.
    """
    return jsonify(
        {
            "state": external_access.state(),
            "networks": list(external_access.describe()),
            "published": _published_ports(),
            "api": external_access.api_mode(),
            "mcp": mcp_server.mode(),
        }
    )


def _served_under() -> str:
    """The path this add-on is reached under, with a trailing slash.

    Home Assistant proxies Ingress under a long prefix and announces it in
    X-Ingress-Path rather than the forwarded-prefix header Flask understands,
    so without reading it every address in the document would be missing the
    prefix and "try it out" would call a page that is not there. Over the
    published port there is no prefix and the root is the answer.
    """
    prefix = request.headers.get("X-Ingress-Path") or request.script_root or ""
    return prefix.rstrip("/") + "/"


@system.route("/api/openapi.json", methods=["GET"])
def openapi_document():
    """The API description: what a token opens, in one machine-readable file."""
    return jsonify(api_spec.document(base_url=_served_under()))


@system.route("/api/docs", methods=["GET"])
def api_docs():
    """The same description, to read and try out. Ingress-only, like the UI."""
    return render_template("api_docs.html", document_url="api/openapi.json")


@system.route("/api/api_token", methods=["GET"])
def api_token_status():
    """Return whether an external API token exists (never the token itself).

    Ingress-only: the access gate refuses direct (non-Ingress) requests here.
    """
    return jsonify(renamer_state["api_token_store"].status())


@system.route("/api/api_token", methods=["POST"])
def api_token_generate():
    """Generate (or replace) the external API token and return it once.

    The plaintext is shown only in this response; only its hash is stored, so it
    cannot be retrieved again. Ingress-only.
    """
    store = renamer_state["api_token_store"]
    token = store.generate()
    result = {"token": token}
    result.update(store.status())
    return jsonify(result)


@system.route("/api/api_token", methods=["DELETE"])
def api_token_revoke():
    """Revoke the external API token, disabling external access. Ingress-only."""
    renamer_state["api_token_store"].revoke()
    return jsonify(renamer_state["api_token_store"].status())
