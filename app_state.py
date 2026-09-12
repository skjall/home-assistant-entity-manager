"""The stores and the mutable state the whole add-on shares.

Every route, job handler and tool works on the same registries and the same
JSON-backed stores; they live here so no part has to import the web layer to
reach them.
"""

import logging
import os

from api_token_store import ApiTokenStore
from device_swap import SwapJobStore
from entity_registry import EntityRegistry
from ha_translations import HaTranslations
from jobs import TERMINAL_STATES, JobStore, JobWorker
from naming_overrides import NamingOverrides
from naming_rules import NamingRules
from naming_templates import NamingTemplates
from rename_log import RenameLog
from type_mappings import DEFAULT_SYSTEM_MAPPINGS, TypeMappings

logger = logging.getLogger(__name__)

# Language-independent constant for entities without area assignment
UNASSIGNED_AREA = "__unassigned__"

# Persistent data directory. Defaults to the add-on's /data mount; overridable
# via DATA_DIR for local runs, tests and CI where /data is not available.
DATA_DIR = os.getenv("DATA_DIR", "/data")

# Global state
# Type rules replace the flat user mappings; the legacy file is migrated once
# and kept as a backup next to a report of what was merged.
ha_translations = HaTranslations()
naming_rules_store = NamingRules(
    os.path.join(DATA_DIR, "naming_rules.json"),
    legacy_path=os.path.join(DATA_DIR, "user_type_mappings.json"),
    device_class_keys=DEFAULT_SYSTEM_MAPPINGS["device_class"].keys(),
)

renamer_state = {
    "client": None,
    "naming_rules": naming_rules_store,
    "restructurer": None,
    "areas": {},
    "entities_by_area": {},
    "proposed_changes": {},
    "naming_overrides": NamingOverrides(os.path.join(DATA_DIR, "naming_overrides.json")),
    "naming_templates": NamingTemplates(os.path.join(DATA_DIR, "naming_templates.json")),
    "type_mappings": TypeMappings(
        user_mappings_path=os.path.join(DATA_DIR, "user_type_mappings.json"),
        rules=naming_rules_store,
        ha_translations=ha_translations,
    ),
    "swap_store": SwapJobStore(os.path.join(DATA_DIR, "device_swaps")),
    "rename_log": RenameLog(os.path.join(DATA_DIR, "rename_log.jsonl")),
    "api_token_store": ApiTokenStore(os.path.join(DATA_DIR, "api_token.json")),
    # Generic background-job infrastructure for long-running operations. Jobs run
    # serially on a single worker thread, off the request path (load_structure
    # rebuilds the restructurer by reassignment and handlers work on snapshots,
    # so no cross-thread lock is needed).
    "job_store": JobStore(os.path.join(DATA_DIR, "jobs"), terminal_states=TERMINAL_STATES),
}
renamer_state["worker"] = JobWorker(renamer_state["job_store"])

# Share the audit log with every EntityRegistry instance so all rename paths
# (single, batch, device cascade) get recorded centrally.
EntityRegistry.rename_log = renamer_state["rename_log"]


def ws_url() -> str:
    """The WebSocket address of Home Assistant, derived from its HTTP one."""
    base_url = os.getenv("HA_URL") or "http://supervisor/core"
    return base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
