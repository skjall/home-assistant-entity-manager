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

# Nothing but Ingress; a published port answers no one.
OFF = "off"
# Private address space, i.e. the home network the add-on is installed in.
LAN = "lan"
# Every caller the port can be reached from, the internet included.
ANY = "any"

POLICIES: Tuple[str, ...] = (OFF, LAN, ANY)

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


def policy() -> str:
    """Return the configured policy, falling back to the strictest one."""
    configured = (os.getenv("EXTERNAL_ACCESS") or "").strip().lower()
    if configured in POLICIES:
        return configured
    if configured:
        logger.warning("Unknown external_access value %r, refusing external access", configured)
    return OFF


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

    An address this add-on cannot make sense of counts as external, so a
    malformed or missing peer never widens access.
    """
    configured = configured or policy()
    if configured == ANY:
        return True
    if configured == LAN:
        return is_local(peer)
    return False
