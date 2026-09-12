"""One reference checker, shared by everything that reads or writes names.

Scanning automations, scripts and dashboards for a reference is expensive, so
the result is cached; every path that changes a name has to say when that cache
is stale.
"""

import logging
import os
from typing import Optional

from json_store import new_lock
from reference_checker import ReferenceChecker

logger = logging.getLogger(__name__)

_reference_checker: Optional[ReferenceChecker] = None
# Requests are served by several threads, so two of them can find the checker
# missing at the same time; without this one would build a second checker with
# an empty cache and the first one's scan would be thrown away.
_lock = new_lock()


def get_reference_checker() -> ReferenceChecker:
    """The one reference checker, built on first use."""
    global _reference_checker
    with _lock:
        if _reference_checker is None:
            _reference_checker = ReferenceChecker(os.getenv("HA_URL"), os.getenv("HA_TOKEN"))
        return _reference_checker


def invalidate_reference_checker_cache():
    """Drop what the checker remembers, so the next question is asked anew."""
    with _lock:
        checker = _reference_checker
    if checker is not None:
        checker.invalidate_cache()
