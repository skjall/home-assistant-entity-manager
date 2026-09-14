"""Who may reach this add-on without going through Home Assistant's Ingress.

Home Assistant proxies the add-on through Ingress, where it has already
authenticated the user. A published port bypasses that entirely, and the
Supervisor gives an add-on no say over the interface its port is bound to: it
always lands on every address of the host. Someone who forwards a port to Home
Assistant and publishes this one has put the add-on on the internet, usually
without meaning to.

So the add-on decides for itself who it answers. The setting is deliberate and
its default refuses everything that does not come through Ingress.
"""

import ipaddress
import logging
import os
from typing import Tuple

logger = logging.getLogger(__name__)

# Written alone, each of these stands for a set of networks, so the common cases
# need no CIDR. Anything else in the setting is a network of its own.
OFF = "off"
# Private address space, i.e. the home network the add-on is installed in.
LAN = "lan"
# Every caller the port can be reached from, the internet included.
ANY = "any"

SHORTHANDS: Tuple[str, ...] = (OFF, LAN, ANY)

# What a token may do once a caller is allowed through: nothing at all, read the
# state, or change it as well. Separate from who may call, because letting a
# script read the log is a different decision from letting it rename entities.
API_OFF = "off"
API_READ = "read"
API_WRITE = "write"

API_MODES: Tuple[str, ...] = (API_OFF, API_READ, API_WRITE)

# The Supervisor proxies Ingress requests from this range.
SUPERVISOR_NETWORK = ipaddress.ip_network("172.30.32.0/23")

# Address space that cannot be routed from the internet.
_LOCAL_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)


# What "any" expands to: everything, both families.
_EVERY_NETWORK = (ipaddress.ip_network("0.0.0.0/0"), ipaddress.ip_network("::/0"))


def entries(configured: str = "") -> Tuple[str, ...]:
    """The setting split into its single entries, in the order given."""
    raw = configured or os.getenv("EXTERNAL_ACCESS") or ""
    parts = raw.replace("\n", ",").replace(";", ",").replace(" ", ",").split(",")
    return tuple(part.strip().lower() for part in parts if part.strip())


def networks(configured: str = "") -> Tuple[ipaddress._BaseNetwork, ...]:
    """The networks a direct caller may come from.

    Each entry is either one of the shorthands or a network of its own, written
    as a CIDR or as a single address. An entry that is neither is dropped with a
    warning: a typo must never widen access, and it must never narrow the rest
    of the list away either.
    """
    resolved = []
    for entry in entries(configured):
        if entry == OFF:
            continue
        if entry == LAN:
            resolved.extend(_LOCAL_NETWORKS)
            continue
        if entry == ANY:
            resolved.extend(_EVERY_NETWORK)
            continue
        try:
            resolved.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning("Ignoring %r in external_access: not a network or address", entry)
    return tuple(resolved)


def state(configured: str = "") -> str:
    """How to describe the setting in one word: off, any, or some networks."""
    allowed = networks(configured)
    if not allowed:
        return OFF
    if any(network in _EVERY_NETWORK for network in allowed):
        return ANY
    return "some"


def describe(configured: str = "") -> Tuple[str, ...]:
    """The configured networks as text, for showing what is actually allowed."""
    return tuple(str(network) for network in networks(configured))


def api_mode() -> str:
    """What a token may do over a published port; reading only when unset."""
    configured = (os.getenv("EXTERNAL_API") or "").strip().lower()
    if configured in API_MODES:
        return configured
    if configured:
        logger.warning("Unknown external_api value %r, allowing reading only", configured)
    return API_READ


def is_supervisor(peer: str) -> bool:
    """True when the request came through the Supervisor, i.e. through Ingress."""
    try:
        return ipaddress.ip_address(peer) in SUPERVISOR_NETWORK
    except ValueError:
        return False


def is_local(peer: str) -> bool:
    """True for an address that cannot be reached from the internet."""
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address in network for network in _LOCAL_NETWORKS)


def allows(peer: str, configured: str = "") -> bool:
    """True when a direct (non-Ingress) request from ``peer`` may be answered.

    An address this add-on cannot make sense of is refused, so a malformed or
    missing peer never widens access.
    """
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    for network in networks(configured):
        if address.version == network.version and address in network:
            return True
    return False
