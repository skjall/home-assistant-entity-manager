"""One reference checker, shared by everything that reads or writes names.

Scanning automations, scripts and dashboards for a reference is expensive, so
the result is cached; every path that changes a name has to say when that cache
is stale.
"""

import logging
import os
from typing import Optional

from reference_checker import ReferenceChecker

logger = logging.getLogger(__name__)

_reference_checker: Optional[ReferenceChecker] = None


def get_reference_checker() -> ReferenceChecker:
    """Get or create the reference checker instance."""
    global _reference_checker
    base_url = os.getenv("HA_URL")
    token = os.getenv("HA_TOKEN")
    if _reference_checker is None:
        _reference_checker = ReferenceChecker(base_url, token)
    return _reference_checker


def invalidate_reference_checker_cache():
    """Invalidate the reference checker cache."""
    if _reference_checker is not None:
        _reference_checker.invalidate_cache()
