"""Whether this add-on may rewrite the user's own configuration files.

The add-on writes entity names through Home Assistant's API, which owns what it
writes. A package, an include or a YAML-mode dashboard is not that: it is a file
the user wrote and maintains, and Home Assistant only ever reads it. Editing it
is a different kind of act, so it is a different kind of decision.

The Supervisor fixes a mount when the container is built, so the add-on holds
write access to the configuration directory whether or not this is switched on.
That is what this setting is for: the permission exists, and the add-on uses it
only where the user has said to.

It is off by default and marked beta, because a mistake here is a configuration
that does not load - and unlike a registry entry, nobody else has a copy.
"""

import logging
import os

logger = logging.getLogger(__name__)

# Nothing is written; a repair says which line to edit and leaves it there.
OFF = "off"
# The add-on rewrites the one line, keeping a copy of the file first.
BETA = "beta"

MODES = (OFF, BETA)

# Where the copy of an edited file goes: the add-on's own data directory, so the
# user's configuration directory stays as tidy as they left it.
BACKUP_DIR = os.path.join(os.getenv("DATA_DIR", "/data"), "yaml_backups")


def mode() -> str:
    """What the user has allowed, defaulting to nothing."""
    configured = (os.getenv("FIX_YAML") or "").strip().lower()
    if configured in MODES:
        return configured
    if configured:
        logger.warning("Unknown fix_yaml value %r, writing nothing", configured)
    return OFF


def allowed() -> bool:
    return mode() == BETA
