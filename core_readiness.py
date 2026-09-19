"""Telling "Home Assistant is not up yet" apart from "something is wrong".

Updating an add-on restarts Home Assistant, and Core takes a while to answer
again. The add-on keeps running through that, so opening it lands on a
Supervisor that has nothing to proxy to yet and answers 502. That is not a
failure anybody can act on - it is a wait - and the panel has been reporting it
as `Error loading data: 502, message='Bad Gateway'`.
"""

import asyncio
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

# What the Supervisor answers while Core is down or coming back: it is there,
# it just has nobody to forward to yet.
NOT_UP_YET = (502, 503, 504)


def core_is_starting(error: BaseException) -> bool:
    """Whether this error means Core is not answering yet, rather than broken.

    A refused connection and a dropped one count as well: Core is restarted in
    place, so the socket goes away before the Supervisor starts answering for
    it.
    """
    if isinstance(error, aiohttp.ClientResponseError):
        return error.status in NOT_UP_YET
    return isinstance(
        error,
        (
            aiohttp.ClientConnectorError,
            aiohttp.ServerDisconnectedError,
            aiohttp.ServerTimeoutError,
            asyncio.TimeoutError,
        ),
    )


def status_of(error: BaseException) -> Optional[int]:
    """The HTTP status behind this error, if it carries one."""
    if isinstance(error, aiohttp.ClientResponseError):
        return error.status
    return None
